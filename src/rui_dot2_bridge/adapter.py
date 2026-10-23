"""rui-side adapter: the Dot 2 as an inbound voice satellite.

This is the object rui's audio plane calls. It owns a :class:`DeviceLink` (the
controller the device dials) and translates between it and the adapter
contract in ``house_ear.device.contract``.

Capabilities are stated honestly: the stock firmware sends one mono 16 kHz
channel and *no* playback reference, so this adapter advertises neither
``REFERENCE`` nor ``MIC_ARRAY``. rui then turns AEC and barge-in off with a
reason, instead of planning features the device cannot do.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import wave
from pathlib import Path
from typing import AsyncIterator

from house_ear.device.contract import (
    AudioFrame,
    Capability,
    DeviceAdapter,
    DeviceInfo,
    Transport,
)

from .device_link import DeviceLink
from .frames import MIC_RATE, SPEAKER_CHUNK_BYTES, SPEAKER_RATE, rechunk_voice
from .volume import VOLUME_CAP_PERCENT, clamp_percent

log = logging.getLogger(__name__)

DEFAULT_TLS_PORT = 8870
DEFAULT_INJECT_PORT = 8871   # loopback-only test hook, off unless DOT2_INJECT_PORT is set
EOS_IDLE_S = 0.4          # a gap this long means the reply is over -> 0x03
PLAY_CHUNK_MS = 20        # re-chunk the device's 80 ms blocks for the plane


class Dot2Adapter(DeviceAdapter):
    """Echo Dot 2 running EchoMuse firmware, driven by rui instead of EchoMuse."""

    def __init__(
        self,
        device_id: str,
        *,
        host: str,
        room: str = "",
        secrets_dir: str | Path = "secrets",
        tls_port: int = DEFAULT_TLS_PORT,
        token: str | None = None,
        volume_cap_percent: float = VOLUME_CAP_PERCENT,
        push_config: bool = True,
        aec_enabled: bool = True,
        oww_on_device: str = "off",
        inject_port: int | None = None,
    ) -> None:
        self.device_id = device_id
        self.host = host
        self.room = room
        self.volume_cap_percent = clamp_percent(volume_cap_percent)
        self._seq = 0
        self._frame_buffer = bytearray()
        self._eos_task: asyncio.Task[None] | None = None
        self._playing = False
        self._link: DeviceLink | None = None
        self._link_kwargs = {
            "certs_dir": Path(secrets_dir),
            "token": token,
            "tls_port": tls_port,
            "plain_port": None,
            "device_id": device_id,
            "volume_cap_percent": self.volume_cap_percent,
            "push_config": push_config,
            "aec_enabled": aec_enabled,
            "oww_on_device": oww_on_device,
        }
        self._inject_port = inject_port
        self._inject_server: asyncio.base_events.Server | None = None
        # Dev-only: tee the microphone to a WAV so a turn can be checked without a
        # person in the room (e.g. feed it to /api/v1/asr). Off unless asked for.
        self._record_path = os.environ.get("DOT2_RECORD") or ""
        self._record_wav: wave.Wave_write | None = None
        self.info = DeviceInfo(
            id=device_id,
            adapter="dot2",
            model="Echo Dot 2 (RS03QR / biscuit)",
            host=host,
            room=room,
            capabilities=frozenset({Capability.PLAYBACK, Capability.VOLUME}),
            transport=Transport.INBOUND,
            channels=1,
            sample_rate=MIC_RATE,
            bit_depth=16,
            mic_channels=1,
            ref_channels=0,
        )

    # ---------------------------------------------------------------- health
    def health(self) -> dict:
        """The plane's `speaker_connected` and the console read this."""
        link = self._link
        if link is None:
            return {"connected": False, "error": "adapter not open"}
        return {
            "connected": link.connected.is_set() and link.stats.data_connects > 0,
            "device_id": link.info.device_id or self.device_id,
            "firmware": link.info.version,
            "ip": link.info.ip,
            "transport": "wss",
            "profile": "mono 16 kHz, no reference (device AEC on)"
            if link.aec_enabled
            else "mono 16 kHz, no reference (device AEC OFF - test mode)",
            "rtt_ms": link.stats.rtt_ms,
            "mic_frames": link.stats.mic_frames,
            "dropped_audio": link.stats.dropped_audio,
            "volume_level": link.volume_level,
            "volume_cap_percent": link.volume_cap_percent,
        }

    def volume_cap(self) -> float:
        return self.volume_cap_percent

    # ---------------------------------------------------------------- contract
    async def probe(self) -> DeviceInfo:
        return self.info

    async def open(self) -> None:
        self._link = DeviceLink(**self._link_kwargs)  # type: ignore[arg-type]
        await self._link.start()
        if self._inject_port:
            from .inject import start_inject_server

            self._inject_server = await start_inject_server(self, "127.0.0.1", self._inject_port)
        log.info(
            "dot2 adapter: waiting for %s on port %s (volume ceiling %.0f%%)",
            self.device_id,
            self._link.bound_ports.get("tls"),
            self.volume_cap_percent,
        )

    async def close(self) -> None:
        if self._eos_task is not None:
            self._eos_task.cancel()
            self._eos_task = None
        if self._inject_server is not None:
            self._inject_server.close()
            await self._inject_server.wait_closed()
            self._inject_server = None
        if self._link is not None:
            await self._link.stop()
            self._link = None

    def _record(self, pcm: bytes) -> None:
        if not self._record_path:
            return
        if self._record_wav is None:
            Path(self._record_path).parent.mkdir(parents=True, exist_ok=True)
            wav = wave.open(self._record_path, "wb")
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(MIC_RATE)
            self._record_wav = wav
        self._record_wav.writeframes(pcm)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        if self._link is None:
            return
        try:
            async for block in self._link.frames():
                self._record(block)
                for piece in rechunk_voice(block, frame_ms=PLAY_CHUNK_MS):
                    if not piece:
                        continue
                    self._seq += 1
                    yield AudioFrame(
                        device_id=self.device_id,
                        seq=self._seq,
                        ts_ms=time.monotonic() * 1000.0,
                        pcm=piece,
                        channels=1,
                        sample_rate=MIC_RATE,
                        reference_channels=0,
                    )
        except asyncio.CancelledError:
            raise

    async def play(self, pcm: bytes, *, sample_rate: int, channels: int) -> None:
        """Queue mono PCM. rui has already resampled it to ``playback.rate``."""
        if channels != 1:
            raise ValueError("the Dot 2 plays mono frames; mix down before play()")
        if sample_rate != SPEAKER_RATE:
            raise ValueError(
                f"the Dot 2 wire is {SPEAKER_RATE} Hz mono; set audio.playback.rate "
                f"to {SPEAKER_RATE} (got {sample_rate})"
            )
        if self._link is None:
            raise RuntimeError("dot2 adapter is not open")
        self._frame_buffer.extend(pcm)
        await self._drain_frames()
        self._playing = True
        self._schedule_eos()

    async def _drain_frames(self) -> None:
        assert self._link is not None
        while len(self._frame_buffer) >= SPEAKER_CHUNK_BYTES:
            chunk = bytes(self._frame_buffer[:SPEAKER_CHUNK_BYTES])
            del self._frame_buffer[:SPEAKER_CHUNK_BYTES]
            await self._link.send_pcm(chunk)

    def _schedule_eos(self) -> None:
        if self._eos_task is not None:
            self._eos_task.cancel()
        self._eos_task = asyncio.create_task(self._eos_after_idle(), name="dot2-eos")

    async def _eos_after_idle(self) -> None:
        try:
            await asyncio.sleep(EOS_IDLE_S)
        except asyncio.CancelledError:
            return
        if not self._playing or self._link is None:
            return
        self._playing = False
        if self._frame_buffer:
            # A short tail (a sentence ending mid-period): pad with silence so the
            # device gets whole 42.7 ms periods, then close the stream.
            tail = bytes(self._frame_buffer) + bytes(SPEAKER_CHUNK_BYTES - len(self._frame_buffer))
            self._frame_buffer.clear()
            await self._link.send_pcm(tail)
        await self._link.end_stream()
        log.debug("dot2 adapter: stream ended (idle %.0f ms)", EOS_IDLE_S * 1000)

    async def stop_playback(self, fade_ms: int = 120) -> None:
        """The device drops everything it has buffered: barge-in stops the sound now."""
        if self._eos_task is not None:
            self._eos_task.cancel()
            self._eos_task = None
        self._playing = False
        self._frame_buffer.clear()
        if self._link is not None:
            await self._link.flush()

    async def set_volume(self, volume: int) -> None:
        """rui's 0-100, capped at the owner's ceiling before it reaches the device."""
        if self._link is None:
            return
        await self._link.set_volume_percent(clamp_percent(volume, self.volume_cap_percent))

    async def set_led(self, effect: str, color: str = "", *, repaint: bool = False) -> None:
        """No LED capability is advertised yet; keep the hook quiet and honest."""
        log.debug("dot2 adapter: set_led(%s) ignored (LED not advertised)", effect)


def build(dev, cfg) -> Dot2Adapter:  # noqa: ANN001 - rui's DeviceConfig/Config
    """rui registry entry point: ``register("dot2", "rui_dot2_bridge.adapter:build")``."""
    import os

    token = None
    token_env = getattr(dev, "token_env", None)
    if token_env:
        token = os.environ.get(token_env) or None
    aec_env = os.environ.get("DOT2_AEC_ENABLED", "1").strip().lower()
    inject = os.environ.get("DOT2_INJECT_PORT", "").strip()
    return Dot2Adapter(
        dev.id,
        host=dev.host,
        room=getattr(dev, "room", ""),
        secrets_dir=(
            getattr(dev, "secrets_dir", None)
            or os.environ.get("DOT2_SECRETS_DIR")
            or "secrets"
        ),
        tls_port=int(getattr(dev, "listen_port", None) or DEFAULT_TLS_PORT),
        token=token,
        volume_cap_percent=float(os.environ.get("DOT2_VOLUME_CAP", VOLUME_CAP_PERCENT)),
        aec_enabled=aec_env not in ("0", "false", "no", "off"),
        inject_port=int(inject) if inject else None,
    )
