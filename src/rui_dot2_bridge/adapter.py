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
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from house_ear.device.contract import (
    AudioFrame,
    Capability,
    DeviceAdapter,
    DeviceInfo,
    Transport,
)

from .control import start_control_server
from .device_link import DeviceLink
from .dsp import LoudnessProcessor
from .frames import MIC_RATE, SPEAKER_CHUNK_BYTES, SPEAKER_RATE
from .volume import VOLUME_CAP_PERCENT, capped_level, clamp_percent

log = logging.getLogger(__name__)

def _rechunk(pcm: bytes, channels: int, frame_ms: int) -> list[bytes]:
    """Cut an interleaved block into ``frame_ms`` pieces, never mid-sample-frame."""
    step = MIC_RATE * 2 * frame_ms // 1000 * channels
    if step <= 0:
        raise ValueError("frame_ms too small")
    return [pcm[i : i + step] for i in range(0, len(pcm), step)]


#: The Dot 2 ring is 12 LEDs (id 0..11). Values are 0-255 per channel and
#: deliberately modest: the ring exists for feedback, not for lighting a room.
LED_COLORS: dict[str, tuple[int, int, int]] = {
    "off": (0, 0, 0),
    "idle": (0, 0, 0),
    "listening": (0, 150, 70),
    "wake": (0, 150, 70),
    "thinking": (0, 40, 160),
    "speaking": (0, 110, 150),
    "error": (170, 0, 0),
    "music": (100, 0, 140),
    "music_paused": (120, 60, 0),
    "muted": (120, 0, 0),
}
LED_COUNT = 12


def _ring(rgb: tuple[int, int, int]) -> list[dict]:
    r, g, b = rgb
    return [{"id": i, "r": r, "g": g, "b": b} for i in range(LED_COUNT)]


class _Dot2Services:
    """The optional device services rui probes: volume now, shell never."""

    def __init__(self, adapter: Dot2Adapter) -> None:
        self._adapter = adapter

    async def shell(self, script: str, timeout: float = 3.0) -> str:
        raise NotImplementedError("the Dot 2 adapter exposes no shell")

    async def read_volume(self) -> int | None:
        return self._adapter.volume_255()

    async def set_volume(self, volume: int) -> None:
        await self._adapter.set_volume_255(int(volume))


DEFAULT_TLS_PORT = 8870
DEFAULT_INJECT_PORT = 8871   # loopback-only test hook, off unless DOT2_INJECT_PORT is set
# A gap this long means the reply is over -> 0x03. 0.4 s was too eager: TTS
# audio arrives in bursts, and any >0.4 s stall inside one reply closed the
# stream and reopened it (device primeWait ~30-800 ms) - heard as a stutter
# mid-sentence (2026-10-24, "小瑞说 2 的时候中间卡了"). The device buffers
# ~300 ms, so 1.2 s of silence still ends the stream long before anything starves.
EOS_IDLE_S = 1.2
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
        mic_channels: int = 1,
        ref_channels: int = 0,
    ) -> None:
        self.device_id = device_id
        self.host = host
        self.room = room
        # The ceiling itself is NOT clamped by the default 30 %: that default is a
        # fallback, not a hard limit. This bug made DOT2_VOLUME_CAP=70 silently
        # behave as 30 and cost a morning of "the speaker is very quiet".
        self.volume_cap_percent = max(0.0, min(float(volume_cap_percent), 100.0))
        # Capture layout. Stock firmware: one mono channel, no reference.
        # P3 (firmware fork) will add a trailing playback reference; the adapter
        # is already able to expose it, so the switch is config, not code.
        self.mic_channels = max(1, int(mic_channels))
        self.ref_channels = max(0, int(ref_channels))
        self.channels = self.mic_channels + self.ref_channels
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
        # +24 dB (the device default) clips normal speech; 18 dB keeps the peaks
        # inside full scale. Override with DOT2_MIC_GAIN_DB=24 to go back.
        self._mic_gain_db = int(os.environ.get("DOT2_MIC_GAIN_DB", "18") or 18)
        # Debug: capture exactly what we hand to the device (DOT2_PLAYBACK_DUMP=dir).
        # Used to tell a gap in our processed PCM from a stream split on the wire.
        self._dump_dir = Path(os.environ.get("DOT2_PLAYBACK_DUMP") or "")
        self._dump_buf = bytearray()
        self._last_chunk_at: float | None = None
        self._inject_server: asyncio.base_events.Server | None = None
        # Loudness: stock EQ ladder + limiter, runtime-tunable over loopback HTTP.
        self._eq_dir = Path(os.environ.get("DOT2_EQ_DIR") or "artifacts/eq-prototype")
        self._loudness_file = Path(os.environ.get("DOT2_LOUDNESS_FILE") or "dev/rui/loudness.json")
        self._control_port = int(os.environ.get("DOT2_CONTROL_PORT", "0") or 0)
        config = LoudnessProcessor.load_config(self._loudness_file)
        env_mode = (os.environ.get("DOT2_LOUDNESS_MODE") or "").strip()
        if env_mode:
            config.mode = env_mode
        if not os.environ.get("DOT2_EQ_DIR"):
            # Default lookup: the evaluation copy, then secrets/eq for a deployment.
            for candidate in ("artifacts/eq-prototype", "secrets/eq"):
                if (Path(candidate) / "EQ_100.cfg").is_file():
                    self._eq_dir = Path(candidate)
                    break
        self._loudness = LoudnessProcessor(config, self._eq_dir)
        self._control_server: asyncio.base_events.Server | None = None
        # Dev-only: tee the microphone to a WAV so a turn can be checked without a
        # person in the room (e.g. feed it to /api/v1/asr). Off unless asked for.
        self._record_path = os.environ.get("DOT2_RECORD") or ""
        self._record_wav: wave.Wave_write | None = None
        capabilities: set[Capability] = {
            Capability.PLAYBACK, Capability.VOLUME, Capability.LED,
        }
        if self.ref_channels > 0:
            capabilities.add(Capability.REFERENCE)
        if self.mic_channels > 1:
            capabilities.add(Capability.MIC_ARRAY)
        self.info = DeviceInfo(
            id=device_id,
            adapter="dot2",
            model="Echo Dot 2 (RS03QR / biscuit)",
            host=host,
            room=room,
            capabilities=frozenset(capabilities),
            transport=Transport.INBOUND,
            channels=self.channels,
            sample_rate=MIC_RATE,
            bit_depth=16,
            mic_channels=self.mic_channels,
            ref_channels=self.ref_channels,
        )

    # ---------------------------------------------------------------- health
    @property
    def services(self) -> _Dot2Services:
        return _Dot2Services(self)

    # ------------------------------------------------------------ volume (255)
    # rui's reconcile path speaks the LX06's mixer scale (0-255) and compares
    # for equality, so both directions have to agree exactly or it rewrites the
    # volume every cycle. At the ceiling the answer is the ceiling's 0-255
    # projection; anywhere else it is the device level scaled across.
    def volume_255(self) -> int | None:
        if self._link is None:
            return None
        ceiling = round(self.volume_cap_percent * 255 / 100)
        if self._link.volume_level == capped_level(self.volume_cap_percent):
            return ceiling
        return round(self._link.volume_level * 255 / 127)

    async def read_volume(self) -> int | None:
        return self.volume_255()

    async def set_volume_255(self, volume: int) -> None:
        await self.set_volume(round(max(0, min(255, volume)) * 100 / 255))

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
            "profile": (
                f"{self.mic_channels} mic + {self.ref_channels} ref @16 kHz "
                f"({'device AEC on' if link.aec_enabled else 'device AEC OFF - test mode'})"
            ),
            "rtt_ms": link.stats.rtt_ms,
            "mic_frames": link.stats.mic_frames,
            "dropped_audio": link.stats.dropped_audio,
            "volume_level": link.volume_level,
            "volume_cap_percent": link.volume_cap_percent,
            "loudness": {
                "enabled": self._loudness.config.enabled,
                "mode": self._loudness.config.mode,
                "curve": self._loudness.curve_name,
            },
        }

    def volume_cap(self) -> float:
        return self.volume_cap_percent

    # ---------------------------------------------------------------- contract
    async def probe(self) -> DeviceInfo:
        return self.info

    async def open(self) -> None:
        self._link_kwargs["mic_gain_db"] = self._mic_gain_db
        self._link = DeviceLink(**self._link_kwargs)  # type: ignore[arg-type]
        await self._link.start()
        if self._inject_port:
            from .inject import start_inject_server

            self._inject_server = await start_inject_server(self, "127.0.0.1", self._inject_port)
        if self._control_port:
            self._control_server = await start_control_server(
                self._loudness, "127.0.0.1", self._control_port, self._loudness_file
            )
        log.info("dot2 adapter: loudness %s", self._loudness.describe())
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
        if self._control_server is not None:
            self._control_server.close()
            await self._control_server.wait_closed()
            self._control_server = None
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
                for piece in _rechunk(block, self.channels, PLAY_CHUNK_MS):
                    if not piece:
                        continue
                    self._seq += 1
                    yield AudioFrame(
                        device_id=self.device_id,
                        seq=self._seq,
                        ts_ms=time.monotonic() * 1000.0,
                        pcm=piece,
                        channels=self.channels,
                        sample_rate=MIC_RATE,
                        reference_channels=self.ref_channels,
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
        # Fresh stream: drop the previous stream's FIR/limiter state, otherwise its
        # tail is mixed into the first milliseconds of this one (a click after "在呢").
        if not self._playing:
            self._loudness.reset_stream()
        # Stock EQ curve follows the device's current level; then EQ + limiter.
        self._loudness.set_level(self._link.volume_level)
        pcm = self._loudness.process_bytes(pcm)
        if self._dump_dir:
            now = time.monotonic()
            gap_ms = 0.0 if self._last_chunk_at is None else (now - self._last_chunk_at) * 1000.0
            self._last_chunk_at = now
            with (self._dump_dir / "calls.log").open("a") as handle:
                handle.write(f"{datetime.now():%H:%M:%S.%f} bytes={len(pcm)} gap_ms={gap_ms:.0f}\n")
            self._dump_buf.extend(pcm)
            if len(self._dump_buf) > 48000 * 2 * 5:  # 5 s per file
                self._flush_dump()
        self._frame_buffer.extend(pcm)
        await self._drain_frames()
        self._playing = True
        self._schedule_eos()

    def _flush_dump(self) -> None:
        if not self._dump_dir or not self._dump_buf:
            return
        try:
            self._dump_dir.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("play-%H%M%S-%f.pcm")
            (self._dump_dir / name).write_bytes(bytes(self._dump_buf))
            log.info("dot2 adapter: dumped %.2f s of outbound PCM to %s",
                     len(self._dump_buf) / 2 / 48000, name)
        finally:
            self._dump_buf.clear()

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
        self._flush_dump()
        log.debug("dot2 adapter: stream ended (idle %.0f ms)", EOS_IDLE_S * 1000)

    async def stop_playback(self, fade_ms: int = 120) -> None:
        """The device drops everything it has buffered: barge-in stops the sound now."""
        if self._eos_task is not None:
            self._eos_task.cancel()
            self._eos_task = None
        self._playing = False
        self._frame_buffer.clear()
        self._flush_dump()
        if self._link is not None:
            await self._link.flush()

    async def set_volume(self, volume: int) -> None:
        """rui's 0-100, capped at the owner's ceiling before it reaches the device."""
        if self._link is None:
            return
        await self._link.set_volume_percent(clamp_percent(volume, self.volume_cap_percent))

    async def set_led(self, effect: str, color: str = "", *, repaint: bool = False) -> None:
        """Paint the 12-LED ring for a rui state. Unknown states go dark, not stale."""
        if self._link is None:
            return
        rgb = LED_COLORS.get(effect, (0, 0, 0))
        if color:
            raw = color.lstrip("#")
            if len(raw) == 6:
                try:
                    rgb = (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
                except ValueError:
                    pass
        await self._link.set_leds(_ring(rgb), listening=effect in ("listening", "wake"))


def build(dev, cfg) -> Dot2Adapter:  # noqa: ANN001 - rui's DeviceConfig/Config
    """rui registry entry point: ``register("dot2", "rui_dot2_bridge.adapter:build")``."""
    import os

    token = None
    token_env = getattr(dev, "token_env", None)
    if token_env:
        token = os.environ.get(token_env) or None
    aec_env = os.environ.get("DOT2_AEC_ENABLED", "1").strip().lower()
    inject = os.environ.get("DOT2_INJECT_PORT", "").strip()
    mic_channels = int(os.environ.get("DOT2_MIC_CHANNELS", "1") or 1)
    ref_channels = int(os.environ.get("DOT2_REF_CHANNELS", "0") or 0)
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
        mic_channels=mic_channels,
        ref_channels=ref_channels,
    )
