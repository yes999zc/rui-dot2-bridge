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

from . import plug as plug_manifest
from .control import start_control_server
from .device_link import DeviceLink
from .dsp import LoudnessConfig, LoudnessProcessor, fade_out_pcm
from .frames import MIC_RATE, SPEAKER_CHUNK_BYTES, SPEAKER_RATE
from .volume import DEVICE_LEVEL_MAX, VOLUME_CAP_PERCENT, clamp_percent

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
# Playback lead: rui hands a reply over in TTS-sized pieces (a first ~200 ms piece,
# then the rest ~400 ms later). Sending the first piece immediately leaves a hole
# before the rest arrives; buffering this much before the first send hides it at
# the cost of the same latency. 0.45 s was picked from the measured 407 ms gap.
PREBUFFER_S = 0.45
PREBUFFER_BYTES = int(SPEAKER_RATE * PREBUFFER_S) * 2
PREBUFFER_START_S = 0.35   # a short reply may never reach the threshold: start anyway

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
        prebuffer_ms: int | None = None,
        prebuffer_start_ms: int | None = None,
        eos_idle_ms: int | None = None,
        loudness_defaults: dict | None = None,
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
        # Device-side AEC/DSP knobs (None = firmware default).
        _tail = os.environ.get("DOT2_AEC_TAIL_MS", "").strip()
        _delay = os.environ.get("DOT2_AEC_DELAY_MS", "").strip()
        _agc = os.environ.get("DOT2_AGC_ENABLED", "").strip().lower()
        _barge = os.environ.get("DOT2_BARGE_IN_ENABLED", "").strip().lower()
        _barge_thr = os.environ.get("DOT2_BARGE_IN_THRESHOLD", "").strip()
        self._aec_tail_ms = int(_tail) if _tail else None
        self._aec_delay_ms = int(_delay) if _delay else None
        self._agc_enabled = None if not _agc else _agc not in ("0", "false", "no", "off")
        self._barge_in_enabled = None if not _barge else _barge not in ("0", "false", "no", "off")
        # float first: a value like "0.05" used to crash int() (review finding)
        self._barge_in_threshold = float(_barge_thr) if _barge_thr else None
        _wake_mic = os.environ.get("DOT2_WAKE_MIC", "").strip()
        self._wake_mic = int(_wake_mic) if _wake_mic else None
        self._aec_ref_source = os.environ.get("DOT2_AEC_REF_SOURCE", "").strip() or None
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
            "ref_uplink": self.ref_channels > 0,
            "aec_tail_ms": self._aec_tail_ms,
            "aec_delay_ms": self._aec_delay_ms,
            "agc_enabled": self._agc_enabled,
            "barge_in_enabled": self._barge_in_enabled,
            "barge_in_threshold": self._barge_in_threshold,
            "wake_mic": self._wake_mic,
            "aec_ref_source": self._aec_ref_source,
            "oww_on_device": oww_on_device,
        }
        self._inject_port = inject_port
        # +24 dB (the device default) clips normal speech; 18 dB keeps the peaks
        # inside full scale. Override with DOT2_MIC_GAIN_DB=24 to go back.
        self._mic_gain_db = int(os.environ.get("DOT2_MIC_GAIN_DB", "18") or 18)
        # Debug: capture exactly what we hand to the device (DOT2_PLAYBACK_DUMP=dir).
        # Used to tell a gap in our processed PCM from a stream split on the wire.
        self._prebuffer_ms = int(
            os.environ.get("DOT2_PREBUFFER_MS")
            or prebuffer_ms
            or int(PREBUFFER_S * 1000)
        )
        self._prebuffer_bytes = int(SPEAKER_RATE * self._prebuffer_ms / 1000) * 2
        self._prebuffer_start_s = float(
            os.environ.get("DOT2_PREBUFFER_START_MS") or prebuffer_start_ms or 350
        ) / 1000.0
        self._eos_idle_s = float(
            os.environ.get("DOT2_EOS_IDLE_MS") or eos_idle_ms or int(EOS_IDLE_S * 1000)
        ) / 1000.0
        self._stream_started = False
        self._prebuffer_task: asyncio.Task | None = None
        # Playback gate: while our own audio is on the speaker, the residual echo
        # is what the controller's VAD/ASR hears as a user turn ("self-hearing").
        # With device AEC alone at 14-18 dB that residual is above the VAD floor.
        # Gating the uplink for a configurable hold after playback makes the plug
        # (speaker side) own the decision instead of asking rui to change.
        _gate = os.environ.get("DOT2_PLAYBACK_GATE_MS", "").strip()
        self._playback_gate_s = (float(_gate) / 1000.0) if _gate else 0.0
        self._last_play_at: float | None = None
        dump = os.environ.get("DOT2_PLAYBACK_DUMP", "").strip()
        self._dump_dir = Path(dump) if dump else None
        uplink_dump = os.environ.get("DOT2_UPLINK_DUMP", "").strip()
        self._uplink_dump = Path(uplink_dump) if uplink_dump else None
        self._uplink_dump_handle = None
        self._uplink_blocks = 0
        self._dump_buf = bytearray()
        self._last_chunk_at: float | None = None
        self._inject_server: asyncio.base_events.Server | None = None
        # Loudness: stock EQ ladder + limiter, runtime-tunable over loopback HTTP.
        self._eq_dir = Path(os.environ.get("DOT2_EQ_DIR") or "artifacts/eq-prototype")
        self._loudness_file = Path(os.environ.get("DOT2_LOUDNESS_FILE") or "dev/rui/loudness.json")
        self._control_port = int(os.environ.get("DOT2_CONTROL_PORT", "0") or 0)
        config = LoudnessProcessor.load_config(self._loudness_file)
        if loudness_defaults and not self._loudness_file.is_file():
            # A fresh deployment inherits the curve declared by the plug manifest;
            # a persisted file (operator/runtime edits) always wins over it.
            config = LoudnessConfig.from_json({**config.to_json(), **loudness_defaults})
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
        """The device's *actual* level, projected onto rui's 0-255 scale.

        Two bugs lived here (audit 2026-10-24): the ceiling comparison called
        ``capped_level()`` without passing the cap, so its default 30 % made a
        level-38 device look like "at the ceiling" and report 100 % whenever the
        real cap was higher; and the value was taken from the target rather than
        the device's last report, so pressing the physical volume key never moved
        the console. Projecting the reported level makes 127 -> 255 and 38 -> 76
        regardless of the configured ceiling.
        """
        if self._link is None:
            return None
        level = getattr(self._link, "reported_level", None)
        if level is None:
            level = self._link.volume_level
        return round(max(0, min(DEVICE_LEVEL_MAX, int(level))) * 255 / DEVICE_LEVEL_MAX)

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
        if self._uplink_dump_handle is not None:
            self._uplink_dump_handle.close()
            self._uplink_dump_handle = None
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
                if self._uplink_dump is not None:
                    if self._uplink_dump_handle is None:
                        self._uplink_dump.parent.mkdir(parents=True, exist_ok=True)
                        self._uplink_dump_handle = self._uplink_dump.open("wb")
                    self._uplink_dump_handle.write(block)
                    self._uplink_blocks += 1
                    if self._uplink_blocks <= 5 or self._uplink_blocks % 100 == 0:
                        log.info("dot2 adapter: uplink block %d bytes=%d channels=%d",
                                 self._uplink_blocks, len(block), self.channels)
                for piece in _rechunk(block, self.channels, PLAY_CHUNK_MS):
                    if not piece:
                        continue
                    if self._playback_gate_s and self._last_play_at is not None:
                        quiet = time.monotonic() - self._last_play_at > self._playback_gate_s
                        # rate is set by the last play() call; EOS/idle does not
                        # update it, so a 0.5 s hold covers the speaker tail.
                        if not quiet:
                            piece = self._gate_mic(piece)
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

    def _gate_mic(self, piece: bytes) -> bytes:
        """Silence the microphone channel(s), keeping the reference intact."""
        if self.ref_channels == 0:
            return bytes(len(piece))
        step = 2 * self.channels
        out = bytearray(piece)
        for i in range(0, len(out) - step + 1, step):
            out[i:i + 2] = b"\x00\x00"  # first channel = mic
        return bytes(out)

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
        self._last_play_at = time.monotonic()
        self._frame_buffer.extend(pcm)
        if not self._stream_started:
            if len(self._frame_buffer) >= self._prebuffer_bytes:
                self._stream_started = True
            else:
                if self._prebuffer_task is None or self._prebuffer_task.done():
                    self._prebuffer_task = asyncio.create_task(
                        self._start_after_prebuffer(), name="dot2-prebuffer")
                self._playing = True
                self._schedule_eos()
                return
        await self._drain_frames()
        self._playing = True
        self._schedule_eos()

    async def _start_after_prebuffer(self) -> None:
        """Short replies may never fill the prebuffer: start after a short wait."""
        try:
            await asyncio.sleep(self._prebuffer_start_s)
        except asyncio.CancelledError:
            return
        if self._stream_started or not self._frame_buffer:
            return
        self._stream_started = True
        await self._drain_frames()

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
        if not self._stream_started:
            return
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
            await asyncio.sleep(self._eos_idle_s)
        except asyncio.CancelledError:
            return
        if not self._playing or self._link is None:
            return
        self._playing = False
        self._stream_started = False
        if len(self._frame_buffer) < int(SPEAKER_RATE * 2 * 0.005):
            # A sub-5 ms remainder after the last whole period would open a 1-period
            # stream at EOS (the "ghost" stream that pollutes primeWait/recvSpan).
            # It is inaudible; drop it rather than play it late.
            self._frame_buffer.clear()
            await self._link.end_stream()
            return
        if self._frame_buffer:
            # Fade the last ~10 ms of the partial tail, then pad with silence so the
            # device gets whole 42.7 ms periods. Holding the tail back (instead of
            # sending it) was tried and reverted: EOS fires 1.2 s after the last
            # chunk, so the held fragment would play after a 1.2 s hole.
            tail = fade_out_pcm(bytes(self._frame_buffer))
            if len(tail) < SPEAKER_CHUNK_BYTES:
                tail += bytes(SPEAKER_CHUNK_BYTES - len(tail))
            self._frame_buffer.clear()
            await self._link.send_pcm(tail)
        await self._link.end_stream()
        self._flush_dump()
        log.debug("dot2 adapter: stream ended (idle %.0f ms)", EOS_IDLE_S * 1000)

    async def stop_playback(self, fade_ms: int = 120) -> None:
        """The device drops everything it has buffered: barge-in stops the sound now.

        rui calls this before the first chunk of *every* reply (process.py:1108).
        The flush must be sent - it is what opens the device's 1.024 s prime
        bypass - but it travels on /control while PCM travels on /data, so
        DeviceLink.flush() waits for a pong on /control before returning. Without
        that barrier the device accepted the first two blocks and then discarded
        them on the late flush: 406 ms hole at the start of 11/11 replies.
        """
        active = self._playing or bool(self._frame_buffer)
        if self._eos_task is not None:
            self._eos_task.cancel()
            self._eos_task = None
        self._playing = False
        self._frame_buffer.clear()
        self._stream_started = False
        if self._prebuffer_task is not None:
            self._prebuffer_task.cancel()
            self._prebuffer_task = None
        self._flush_dump()
        if self._link is not None:
            await self._link.flush()
            if active:
                # The flush discards buffered audio but leaves the device's
                # discarding state set; the EOS clears it. Without this a cue
                # stopped mid-play could swallow the next reply's first frames
                # (2026-10-24 review). Both ride the data socket, in order.
                await self._link.end_stream()

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
    mute = os.environ.get("DOT2_MUTE", "").strip().lower() in ("1", "true", "yes", "on")
    # The plug manifest supplies deployment defaults; env wins over it, and rui's
    # DeviceConfig wins over both for the fields rui owns (host, port, secrets).
    manifest = plug_manifest.load()
    aec_manifest = str(plug_manifest.get(manifest, "capabilities", "aec", default="device"))
    aec_env = os.environ.get("DOT2_AEC_ENABLED", "1" if aec_manifest != "none" else "0").strip().lower()
    inject = os.environ.get("DOT2_INJECT_PORT", "").strip()
    mic_channels = int(
        os.environ.get("DOT2_MIC_CHANNELS")
        or plug_manifest.get(manifest, "audio", "capture", "mic_channels", default=1)
    )
    ref_channels = int(
        os.environ.get("DOT2_REF_CHANNELS")
        or plug_manifest.get(manifest, "audio", "capture", "ref_channels", default=0)
    )
    mic_gain_db = int(
        os.environ.get("DOT2_MIC_GAIN_DB")
        or plug_manifest.get(manifest, "audio", "capture", "mic_gain_db", default=18)
    )
    if os.environ.get("DOT2_MIC_GAIN_DB"):
        os.environ.setdefault("DOT2_MIC_GAIN_DB", str(mic_gain_db))
    return Dot2Adapter(
        dev.id,
        host=dev.host,
        room=getattr(dev, "room", ""),
        secrets_dir=(
            getattr(dev, "secrets_dir", None)
            or os.environ.get("DOT2_SECRETS_DIR")
            or "secrets"
        ),
        tls_port=int(
            getattr(dev, "listen_port", None)
            or plug_manifest.get(manifest, "transport", "port", default=DEFAULT_TLS_PORT)
        ),
        token=token,
        # Mute = the ceiling itself becomes 0, so every path (connect, reconciler,
        # physical buttons) lands on level 0 and the device stays silent while
        # wake/ASR/barge keep working. Flip DOT2_MUTE off and restart to unmute.
        volume_cap_percent=0.0 if mute else float(
            os.environ.get("DOT2_VOLUME_CAP")
            or plug_manifest.get(manifest, "volume", "ceiling_percent", default=VOLUME_CAP_PERCENT)
        ),
        aec_enabled=aec_env not in ("0", "false", "no", "off"),
        inject_port=int(inject) if inject else None,
        mic_channels=mic_channels,
        ref_channels=ref_channels,
        prebuffer_ms=plug_manifest.get(manifest, "audio", "playback", "prebuffer_ms"),
        prebuffer_start_ms=plug_manifest.get(manifest, "audio", "playback", "prebuffer_start_ms"),
        eos_idle_ms=plug_manifest.get(manifest, "audio", "playback", "eos_idle_ms"),
        loudness_defaults=manifest.get("loudness") if isinstance(manifest.get("loudness"), dict) else None,
    )
