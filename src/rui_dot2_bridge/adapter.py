"""rui-side adapter: the Dot 2 as an inbound voice satellite.

This is the object rui's audio plane calls. It owns a :class:`DeviceLink` (the
controller the device dials) and translates between it and the Socket & Plug v1
contract in ``rui.device.contract`` (docs/socket-plug-v1.md at rui 0.3.4).

Capabilities are stated honestly: the stock firmware sends one mono 16 kHz
channel and *no* playback reference, so this adapter advertises neither
``REFERENCE`` nor ``MIC_ARRAY``. rui then turns AEC off with a reason, instead
of planning features the device cannot do. ``DEVICE_WAKE``/``BARGE_VIA_DEVICE``
are only reported by ``probe()`` once the live link proves the device can score
our wake word (manifest intersect probe is the v1 gate).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import wave
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from rui.device.contract import (
    MAX_PENDING_WAKES,
    AudioFrame,
    BoundedWakeQueue,
    Capability,
    DeviceAdapter,
    DeviceInfo,
    DeviceWake,
    Transport,
    device_volume_to_percent,
    percent_to_device_volume,
)

from . import plug as plug_manifest
from .control import start_control_server
from .device_link import DeviceLink
from .dsp import LoudnessConfig, LoudnessProcessor, fade_out_pcm
from .frames import MIC_RATE, SPEAKER_CHUNK_BYTES, SPEAKER_RATE
from .volume import DEVICE_LEVEL_MAX, VOLUME_CAP_PERCENT, clamp_percent

log = logging.getLogger(__name__)

# P0b-C2: how old the last proof of a live data plane may be before `health()`
# stops calling the speaker connected. The device streams mic frames every
# 80 ms; a successfully written PCM frame is the other direction of the same
# proof. 45 s matches the bridge watchdog (three missed 15 s pings), so a power
# cut flips /health, and rui's healthcheck with it, inside the same window -
# without making a quiet-but-healthy room look dead.
SPEAKER_STALE_S = 45.0


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
        # Device mixer unit (0-255): the adapter validates it, no silent coercion.
        await self._adapter.set_volume_255(volume)  # type: ignore[arg-type]


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

#: How long the mic may stay live after the last playback chunk. 0.0 (the
#: default since plug 1.1.3) mutes the uplink from the first frame after the last
#: chunk: stock Dot 2 firmware has no playback reference, so the microphone hears
#: our own speaker. rui's VAD keeps a hold buffer during the ack/echo window and
#: replays it when the hold lifts, which turns a cue echo into a fake "request".
#: Barge-in does not depend on this window: it arrives as a device wake
#: (barge_via_device) and stop_playback() opens the gate immediately.
PLAYBACK_LIVE_S = 0.0

#: Default post-playback mute window. 2500 ms matches the rui 0.5.3 deployment
#: field `devices[].audio.voice.post_playback_mute_ms` used for the Dot 2 and
#: covers rui's follow-up window. `DOT2_PLAYBACK_GATE_MS=0` disables it.
DEFAULT_PLAYBACK_GATE_MS = 2500

EOS_IDLE_S = 1.2
PLAY_CHUNK_MS = 20        # re-chunk the device's 80 ms blocks for the plane
#: Streams at or below this many seconds of content are cues/fillers ("在呢" is
#: 490 ms), which arrive as one short file. They get a short EOS idle so their
#: tail goes out promptly; a long reply keeps the 1.2 s idle that bridges the
#: gaps between streamed sentences.
SHORT_STREAM_S = 1.5
SHORT_STREAM_EOS_IDLE_S = 0.15   # a cue is a finished file: flush it close
#: A tail shorter than this is dropped at EOS instead of being played late.
#: EOS fires one EOS_IDLE after the last full period, so the fragment would land
#: 1.2 s after the reply: the device opens a one-period stream and its onset is
#: an audible click (heard after the 490 ms "在呢" cue: 11 periods + 20.7 ms).
#: Under 30 ms nothing intelligible is lost; a longer tail is faded in and out.
EOS_TAIL_DROP_MS = 30.0

#: Default env var holding the EchoMuse X-EM-Token (inner auth). It must NOT be
#: rui's own transport token (RUI_DEVICE_TOKEN is a different secret); rui's
#: manifest-derived runtime default resolves DeviceConfig.token_env to this name.
DEFAULT_TOKEN_ENV = "DOT2_EM_TOKEN"
#: Device-side wake is opt-in. Stock EchoMuse ships no Chinese OWW model, so a
#: deployment must name the model its firmware runs before it is enabled.
DEFAULT_OWW_MODEL = ""
#: rui probes once right after open() and caches the device_wake decision. If the
#: device is already dialing, this bounded wait lets it register first; it stays
#: well under rui's 5 s probe timeout and only applies when wake is configured.
PROBE_WAKE_WAIT_S = 1.0

#: Shared, read-only vendor EQ curves (C-001 §1). Multiple planes may point at
#: the same directory: it is input, not per-device state. A deployment that
#: mounts its own curve set sets params.eq_dir; otherwise the evaluation copy
#: wins and secrets/eq is the last resort.
DEFAULT_EQ_DIR = "artifacts/eq-prototype"
FALLBACK_EQ_DIR = "secrets/eq"


def _configured_path(explicit: str | Path | None, env_name: str, default: Path) -> Path:
    """C-001 §2: explicit value > DOT2_* env > the device-level default.

    ``None`` (or an empty value) means "nothing was configured for this device":
    a direct ctor and build() after empty params/env both land here, and the env
    fallback keeps a pre-C-001 single-device deployment working unchanged.
    """
    for value in (explicit, os.environ.get(env_name)):
        text = str(value).strip() if value is not None else ""
        if text:
            return Path(text).expanduser()
    return default


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
        oww_model: str = DEFAULT_OWW_MODEL,
        plug_id: str = "",
        inject_port: int | None = None,
        mic_channels: int = 1,
        mic_gain_db: int | None = None,
        ref_channels: int = 0,
        prebuffer_ms: int | None = None,
        prebuffer_start_ms: int | None = None,
        eos_idle_ms: int | None = None,
        loudness_defaults: dict | None = None,
        # C-001 per-device runtime knobs. ``None`` = "not resolved for this
        # device": fall back to the DOT2_* env, then to the device-level default,
        # so a direct ctor keeps the 0.7.9 single-device behaviour and build()
        # supplies the params > env resolution.
        eq_dir: str | Path | None = None,
        loudness_file: str | Path | None = None,
        control_port: int | None = None,
        oww_threshold: float | str | None = None,
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
        # Device-side wake. Off by default: the firmware can only score a wake
        # word whose OWW model was pushed to the device, and stock EchoMuse has
        # no model for ours. A deployment opts in by naming the model via
        # params.oww_model / DOT2_OWW_MODEL; only then do we push owwOnDevice.
        self.oww_on_device = (oww_on_device or "off").strip().lower()
        self.oww_model = str(oww_model or "").strip()
        self._push_oww = bool(self.oww_model)
        # The bar a device-side score must clear. None keeps the firmware default
        # (0.5); a deployment that installed a model may tune it. C-001: the value
        # is per device (params.oww_threshold > DOT2_OWW_THRESHOLD > None), and
        # the kwarg only falls back to env when build() passed nothing.
        _oww_thr = oww_threshold
        if _oww_thr is None:
            _oww_thr = os.environ.get("DOT2_OWW_THRESHOLD", "").strip()
        try:
            _oww_thr_text = str(_oww_thr).strip()
            self.oww_threshold = float(_oww_thr_text) if _oww_thr_text else None
        except (TypeError, ValueError):
            self.oww_threshold = None
            log.warning("dot2 adapter: ignoring oww_threshold=%r (not a number)", _oww_thr)
        self.plug_id = str(plug_id or "").strip()
        # Bounded, thread-safe queue for device wakes (v1 section 4.2). The
        # control socket produces; rui's audio plane drains via drain_wakes().
        self._wakes = BoundedWakeQueue(MAX_PENDING_WAKES)
        self._closed = False
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
            "oww_on_device": self.oww_on_device if self._push_oww else None,
            "oww_model": self.oww_model if self._push_oww else None,
            "oww_threshold": self.oww_threshold,
            # A stream-mode device cannot flag barge (only a private listen session
            # carries that bit), so the link asks us: our own playback state is the
            # thing that makes a wake an interruption rather than a fresh turn.
            "playing": lambda: self._playing,
            "wake_queue": self._wakes,
            "wake_word_enabled": self.oww_on_device == "on",
            # The link owns no outbound PCM queue: the half-sentence lives in
            # `_frame_buffer` here. When the socket dies (write timeout, device
            # close, controller stop) the link tells us so the tail cannot be
            # prepended to the next reply after a reconnect.
            "on_link_lost": self._on_link_lost,
        }
        self._inject_port = inject_port
        # +24 dB (the device default) clips normal speech; 18 dB keeps the peaks
        # inside full scale. Override with DOT2_MIC_GAIN_DB=24 to go back.
        self._mic_gain_db = int(
            mic_gain_db
            if mic_gain_db is not None
            else (os.environ.get("DOT2_MIC_GAIN_DB", "18") or 18)
        )
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
        self._stream_bytes = 0
        self._prebuffer_task: asyncio.Task | None = None
        # Playback gate (plug 1.1.3): the stock Dot 2 has no playback reference,
        # so its microphone hears our own speaker. rui's VAD does not discard that
        # echo: UtteranceGate.hold() buffers the ack window and replays it when the
        # hold lifts, so a residual cue becomes a fake user turn ("self-hearing").
        # rui 0.5.3's ack_echo_ignore_ms only changes how long the buffer is held.
        # The plug therefore owns the fix at the source: the uplink is muted while
        # our audio is on the speaker and stays muted for the post-playback window.
        # Barge-in is unaffected -- it rides the device-wake channel
        # (barge_via_device) and stop_playback() opens the gate immediately.
        # DOT2_PLAYBACK_LIVE_MS=350 restores the old "live during playback + drain"
        # window for diagnostics; DOT2_PLAYBACK_GATE_MS=0 disables the mute.
        _gate = os.environ.get("DOT2_PLAYBACK_GATE_MS", "").strip()
        self._playback_gate_s = (
            float(_gate) / 1000.0 if _gate else DEFAULT_PLAYBACK_GATE_MS / 1000.0
        )
        _live = os.environ.get("DOT2_PLAYBACK_LIVE_MS", "").strip()
        self._playback_live_s = (float(_live) / 1000.0) if _live else PLAYBACK_LIVE_S
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
        # C-001: build() hands in the per-device resolution (params > env); each
        # kwarg only falls back to env/default when it is None. The one writable
        # piece of state - the loudness config - defaults to a per-device path so
        # two planes cannot overwrite each other; EQ curves stay shared because
        # they are read-only vendor input.
        self._eq_dir = _configured_path(eq_dir, "DOT2_EQ_DIR", Path(DEFAULT_EQ_DIR))
        self._loudness_file = _configured_path(
            loudness_file,
            "DOT2_LOUDNESS_FILE",
            Path("dev") / "rui" / device_id / "loudness.json",
        )
        self._control_port = max(
            0,
            int(
                control_port
                if control_port is not None
                else (os.environ.get("DOT2_CONTROL_PORT", "").strip() or 0)
            ),
        )
        config = LoudnessProcessor.load_config(self._loudness_file)
        if loudness_defaults and not self._loudness_file.is_file():
            # A fresh deployment inherits the curve declared by the plug manifest;
            # a persisted file (operator/runtime edits) always wins over it.
            config = LoudnessConfig.from_json({**config.to_json(), **loudness_defaults})
        env_mode = (os.environ.get("DOT2_LOUDNESS_MODE") or "").strip()
        if env_mode:
            config.mode = env_mode
        _explicit_eq = str(eq_dir).strip() if eq_dir is not None else ""
        if not _explicit_eq and not os.environ.get("DOT2_EQ_DIR", "").strip():
            # Default lookup: the evaluation copy, then secrets/eq for a deployment.
            for candidate in (DEFAULT_EQ_DIR, FALLBACK_EQ_DIR):
                if (Path(candidate) / "EQ_100.cfg").is_file():
                    self._eq_dir = Path(candidate)
                    break
        self._loudness = LoudnessProcessor(config, self._eq_dir)
        self._control_server: asyncio.base_events.Server | None = None
        # Dev-only: tee the microphone to a WAV so a turn can be checked without a
        # person in the room (e.g. feed it to /api/v1/asr). Off unless asked for.
        self._record_path = os.environ.get("DOT2_RECORD") or ""
        self._record_wav: wave.Wave_write | None = None
        # Base capabilities only. DEVICE_WAKE/BARGE_VIA_DEVICE are added by
        # probe() when (and only when) the live link proves the device can score
        # our wake word: manifest intersect probe is the v1 gate, so an
        # optimistic claim here would activate a path that can never fire.
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
        """Mixer value in the v1 low-level unit (0-255), or None if unreadable."""
        return self.volume_255()

    async def set_volume_255(self, volume: int) -> None:
        """Low-level mixer write, 0-255 (DeviceServices unit).

        v1 section 4.1: implementations reject out-of-range values instead of
        silently clamping, and the 0-100 config/UI unit is converted only via
        the contract helpers. 0 is in range; config volume=0 means 'unmanaged'
        so core never sends it.
        """
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 255:
            raise ValueError(f"device volume must be an int in 0..255 (got {volume!r})")
        if self._link is None:
            return
        percent = device_volume_to_percent(volume)
        await self._link.set_volume_percent(clamp_percent(percent, self.volume_cap_percent))

    def health(self) -> dict:
        """The plane's `speaker_connected` and the console read this.

        `connected` used to be `Event.is_set() and data_connects > 0`, which is
        sticky: a socket that stays open after a power cut keeps reporting true
        for as long as TCP does (887 s in the field). It is now a freshness
        claim: the gate is set, a data plane is actually adopted, and the last
        proof of life (a received mic frame or a written PCM frame) is younger
        than SPEAKER_STALE_S. The bridge watchdog normally drops the link first;
        this covers a live control socket whose data plane went quiet.
        """
        link = self._link
        if link is None:
            return {"connected": False, "error": "adapter not open"}
        stats = link.stats
        data_plane = bool(getattr(link, "data_plane_up", False))
        last_mic = float(getattr(stats, "last_mic_at", 0.0) or 0.0)
        last_pcm = float(getattr(stats, "last_pcm_sent_at", 0.0) or 0.0)
        last_activity = max(last_mic, last_pcm)
        idle_s = time.monotonic() - last_activity if last_activity > 0.0 else None
        fresh = idle_s is not None and idle_s < SPEAKER_STALE_S
        connected = (
            link.connected.is_set()
            and int(getattr(stats, "data_connects", 0)) > 0
            and data_plane
            and fresh
        )
        return {
            "connected": connected,
            # Diagnostics for the console/log: why the flag above is what it is.
            "data_plane": data_plane,
            "speaker_fresh": fresh,
            "data_idle_ms": None if idle_s is None else round(idle_s * 1000.0, 1),
            "speaker_stale_s": SPEAKER_STALE_S,
            "half_open_drops": int(getattr(stats, "half_open_drops", 0)),
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
            "plug_id": self.plug_id,
            # Playback mute policy: the plugin-side answer to self-hearing.
            "playback_gate_ms": round(self._playback_gate_s * 1000.0),
            "playback_live_ms": round(self._playback_live_s * 1000.0),
            # Device-side wake is reported honestly: available only with a live
            # link that proved local scoring; queue counters are diagnostic.
            "device_wake": {
                "oww_on_device": self.oww_on_device,
                "oww_model": self.oww_model,
                "oww_threshold": self.oww_threshold,
                "available": link.device_wake_available,
                "scoring_evidence": bool(getattr(link, "scoring_evidence", False)),
                "shadow_crossings": int(getattr(link.stats, "shadow_crossings", 0)),
                "pending": len(self._wakes),
                "dropped": self._wakes.dropped,
                "events": link.stats.wake_events,
                "invalid": link.stats.invalid_wakes,
            },
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
        """Report the live capabilities; device_wake needs proof, not intent.

        The manifest declares what the plug enables; probe() reports what the
        connected device actually proved. DEVICE_WAKE/BARGE_VIA_DEVICE are added
        only when DeviceLink saw evidence of on-device scoring: either
        ``listen_state="local"`` (the scorer owns the mic - needs listen sessions)
        or a real ``oww_wake`` / ``oww_shadow_cross`` (the device ran the ONNX
        scorer on our stream). In the streaming path the microphone stays with
        rui, so the device-detected crossing is the wake and the stream is the
        turn audio. Otherwise RUI stays on its own KWS path by design.
        """
        capabilities = set(self.info.capabilities)
        link = self._link
        if link is not None and self._push_oww and not link.connected.is_set():
            # Honest, bounded: only a device that is already dialing can register
            # in time. A device that connects later is covered by a re-probe.
            await link.wait_connected(timeout=PROBE_WAKE_WAIT_S)
        if link is not None and link.device_wake_available:
            capabilities.add(Capability.DEVICE_WAKE)
            capabilities.add(Capability.BARGE_VIA_DEVICE)
        if capabilities == set(self.info.capabilities):
            return self.info
        return replace(self.info, capabilities=frozenset(capabilities))

    # ------------------------------------------------------- device wakes
    def drain_wakes(self) -> list[DeviceWake]:
        """Return every pending device wake once; synchronous and non-blocking.

        RUI validates score/threshold/staleness itself and owns the turn/barge
        decision. Vendor assistant wakes stay on drain_signals(); the two
        channels are never merged (v1 section 4.3).
        """
        if self._closed:
            return []
        return self._wakes.drain()

    def drain_signals(self) -> list:
        """Vendor assistant channel: EchoMuse vendor wakes are not our wake.

        Deliberately empty. oww_wake is the device detecting OUR wake word and
        goes through drain_wakes(); the vendor's own assistant (wake/dialog_end/
        playing) would use this method, and this plug never pretends to have one.
        """
        return []

    def inject_wake(
        self,
        *,
        ts_ms: float | None = None,
        score: float = 0.9,
        threshold: float = 0.5,
        barge: bool = False,
        level: float | None = None,
    ) -> None:
        """Kit-only hook (docs/plug-test-kit.md); not part of DeviceAdapter.

        Production wakes come from the device via oww_wake on the control
        socket. The contract kit uses this to verify the drain/idle/barge path
        without a speaker; it must never be exposed to a device or user input.
        """
        if not isinstance(barge, bool):
            # DeviceWake also rejects this, but an explicit error names the hook.
            raise TypeError(f"inject_wake barge must be a bool (got {barge!r})")
        self._wakes.push(
            DeviceWake(
                ts_ms=time.monotonic() * 1000.0 if ts_ms is None else float(ts_ms),
                score=float(score),
                threshold=float(threshold),
                barge=barge,
                level=None if level is None else float(level),
            )
        )

    async def open(self) -> None:
        self._closed = False
        self._wakes.clear()
        self._reset_stream_state()
        self._link_kwargs["mic_gain_db"] = self._mic_gain_db
        self._link = DeviceLink(**self._link_kwargs)  # type: ignore[arg-type]
        await self._link.start()
        try:
            if self._inject_port:
                from .inject import start_inject_server

                self._inject_server = await start_inject_server(
                    self, "127.0.0.1", self._inject_port
                )
            if self._control_port:
                try:
                    self._control_server = await start_control_server(
                        self._loudness, "127.0.0.1", self._control_port, self._loudness_file
                    )
                except OSError as exc:
                    # C-001 §4: a loopback port already claimed by another plane is a
                    # degraded tuning surface, not a failed device. The inbound link
                    # (and the dev inject hook, if any) is already listening, so
                    # open() must return and let the audio plane run; close() reaps
                    # what did start. EADDRINUSE is the expected case here.
                    self._control_server = None
                    log.warning(
                        "dot2 adapter: loudness control on 127.0.0.1:%d unavailable (%s); "
                        "continuing without it "
                        "(give every plane a unique params.control_port or 0)",
                        self._control_port,
                        exc,
                    )
        except BaseException:
            # Anything else that fails after the inbound link started (a taken
            # inject port, a cancellation) must not leave that link listening for
            # a plane that never came up.
            await self.close()
            raise
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
        self._reset_stream_state()
        # close() must be final: no event from before or after this may be drained
        # by rui after the device is gone (v1 section 4.2).
        self._wakes.clear()
        self._closed = True

    def _on_link_lost(self, reason: str) -> None:
        """Drop the in-flight half sentence; called by DeviceLink (sync)."""
        if self._playing or self._frame_buffer or self._stream_started:
            log.warning("dot2 adapter: link lost (%s); dropping buffered playback", reason)
        self._reset_stream_state()

    def _reset_stream_state(self) -> None:
        """Forget a stream the previous connection never finished.

        ``_frame_buffer`` is the only outbound queue (the link sends straight to
        the socket), so a stale tail here is exactly what would replay into the
        next connection. EOS and prebuffer tasks are derived from that state and
        must be cancelled with it.
        """
        current = asyncio.current_task()
        eos_task = self._eos_task
        self._eos_task = None
        if eos_task is not None and eos_task is not current:
            eos_task.cancel()
        prebuffer_task = self._prebuffer_task
        self._prebuffer_task = None
        if prebuffer_task is not None and prebuffer_task is not current:
            prebuffer_task.cancel()
        self._playing = False
        self._stream_started = False
        self._stream_bytes = 0
        self._frame_buffer.clear()
        # A dead link is not playing, so the uplink gate must open immediately.
        self._last_play_at = None
        self._flush_dump()

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
                    if self._silence_window_active(time.monotonic()):
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

    def _silence_window_active(self, now: float) -> bool:
        """Whether the uplink mic must be muted for playback self-hearing at `now`.

        Active from the first frame after the last `play()` chunk until the gate
        expires -- with the default live window of 0 that is the whole playback
        plus the post-playback window. A positive `DOT2_PLAYBACK_LIVE_MS` keeps
        the mic open for that long after the last chunk; that restores the old
        behaviour and lets the cue echo back into rui's hold buffer, so it is a
        diagnostic escape hatch, not the shipping mode.
        """
        if not self._playback_gate_s or self._last_play_at is None:
            return False
        elapsed = now - self._last_play_at
        return self._playback_live_s < elapsed <= self._playback_gate_s

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
            self._stream_bytes = 0
        self._stream_bytes += len(pcm)
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
        if not await self._drain_frames():
            # The data plane vanished under us; send_pcm dropped the chunks and
            # reset the stream, so do not schedule an EOS for a dead link.
            self._reset_stream_state()
            return
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
        if not await self._drain_frames():
            self._reset_stream_state()

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

    async def _drain_frames(self) -> bool:
        assert self._link is not None
        if not self._stream_started:
            return True
        while len(self._frame_buffer) >= SPEAKER_CHUNK_BYTES:
            chunk = bytes(self._frame_buffer[:SPEAKER_CHUNK_BYTES])
            del self._frame_buffer[:SPEAKER_CHUNK_BYTES]
            if await self._link.send_pcm(chunk) is False:
                return False
        return True

    def _eos_delay(self) -> float:
        short_bytes = int(SPEAKER_RATE * 2 * SHORT_STREAM_S)
        return SHORT_STREAM_EOS_IDLE_S if self._stream_bytes <= short_bytes else self._eos_idle_s

    def _schedule_eos(self) -> None:
        if self._eos_task is not None:
            self._eos_task.cancel()
        self._eos_task = asyncio.create_task(self._eos_after_idle(), name="dot2-eos")

    async def _eos_after_idle(self) -> None:
        try:
            await asyncio.sleep(self._eos_delay())
        except asyncio.CancelledError:
            return
        if not self._playing or self._link is None:
            return
        # A short stream can be shorter than the prebuffer: never end before the
        # prebuffer has actually started it, or the whole buffer is mistaken for a
        # tail (and later played late).
        while self._playing and not self._stream_started and self._frame_buffer:
            await asyncio.sleep(0.05)
        if not self._playing:
            return
        self._playing = False
        self._stream_started = False
        drop_bytes = int(SPEAKER_RATE * 2 * EOS_TAIL_DROP_MS / 1000.0)
        short_stream = self._stream_bytes <= int(SPEAKER_RATE * 2 * SHORT_STREAM_S)
        if len(self._frame_buffer) < drop_bytes and not short_stream:
            # Long reply: cutting the last period is inaudible (the word is long
            # over) and beats a fragment played one idle later. A SHORT cue is the
            # opposite: the cut lands on the last syllable ("在呢" -> the click
            # heard with "呢"), so there the tail is played, windowed, and its
            # short EOS idle keeps it close to the word (2026-10-25).
            self._frame_buffer.clear()
            self._stream_bytes = 0
            await self._link.end_stream()
            return
        if self._frame_buffer:
            # The tail continues the same stream (the tail is played straight after
            # the last whole period, no gap), so it must NOT be faded in: an onset
            # ramp there modulates the last syllable and is heard as a tick ("在呢",
            # 2026-10-25). Only the true end of the audio is faded out, then padded
            # with silence to a whole 42.7 ms period.
            tail = fade_out_pcm(bytes(self._frame_buffer))
            if len(tail) < SPEAKER_CHUNK_BYTES:
                tail += bytes(SPEAKER_CHUNK_BYTES - len(tail))
            self._frame_buffer.clear()
            if await self._link.send_pcm(tail) is False:
                self._reset_stream_state()
                return
        self._stream_bytes = 0
        await self._link.end_stream()
        self._flush_dump()
        log.debug("dot2 adapter: stream ended (idle %.0f ms)", self._eos_delay() * 1000)

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
        # A stop/barge means the user is speaking now: open the uplink gate
        # immediately instead of keeping it muted for the rest of the window.
        # rui also calls this before the first chunk of every reply, where the
        # following play() sets a fresh timestamp.
        self._last_play_at = None
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

    async def set_volume(self, percent: int) -> None:
        """High-level volume, 0-100, same unit as config/UI (v1 section 4.1).

        Maps through percent_to_device_volume() into the 0-255 services surface,
        which then applies the deployment ceiling to the device's own 0-127
        codec scale. Out-of-range input raises rather than silently clamping.
        """
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            raise ValueError(f"volume percent must be an int in 0..100 (got {percent!r})")
        if self._link is None:
            return
        await self.services.set_volume(percent_to_device_volume(percent))

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


#: Production defaults mirroring plug.json. They are used when the legacy JSON
#: manifest is not shipped next to the package (the downstream image copies only
#: src/rui_dot2_bridge, and a quiet 30 % ceiling there was a real deployment bug).
DEFAULT_VOLUME_CAP_PERCENT = 100.0
DEFAULT_PREBUFFER_MS = 450
DEFAULT_PREBUFFER_START_MS = 350
DEFAULT_EOS_IDLE_MS = 1200
DEFAULT_MIC_GAIN_DB = 18


def _params(dev: object | None) -> dict[str, Any]:
    """DeviceConfig.params, tolerating the contract kit fake device and old configs."""
    value = getattr(dev, "params", None)
    return dict(value) if isinstance(value, Mapping) else {}


def _text(*values: object) -> str:
    """First non-empty value as stripped text (empty string when none)."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _optional_int(*values: object) -> int | None:
    """First present value as int; booleans and empty strings are skipped."""
    for value in values:
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"expected an integer, got {value!r}") from None
    return None


def _optional_float(*values: object) -> float | None:
    """First present value as float; booleans and empty strings are skipped."""
    for value in values:
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"expected a number, got {value!r}") from None
    return None


def _flag(*values: object) -> bool:
    """First parsable boolean; an unset/malformed value is False."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    return False


def build(dev: object | None = None, cfg: object | None = None) -> Dot2Adapter:
    """rui registry entry: RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build.

    Precedence, highest first (v1 sections 4.1 and 4.4):

    1. the operator DeviceConfig (``dev``) for the fields rui owns;
    2. the plug non-secret ``params`` (host/room/listen_port/token_env/plug_id,
       oww_model/oww_on_device) - explicitly configured values win over env;
    3. DOT2_* environment fallbacks, then the legacy plug.json defaults.

    ``cfg`` exists for the builder signature only: rui has already resolved the
    device fields and ``params.plug_id`` before calling, and this plug needs no
    global config. Both arguments are optional because the contract kit builds
    with its fake device/config objects.
    """
    env = os.environ
    manifest = plug_manifest.load()
    params = _params(dev)

    # -- identity and inbound transport ---------------------------------------
    device_id = _text(getattr(dev, "id", None), params.get("id"), "dot2")
    host = _text(
        getattr(dev, "host", None),
        params.get("host"),
        env.get("DOT2_HOST"),
        "127.0.0.1",
    )
    room = _text(getattr(dev, "room", None), params.get("room"), env.get("DOT2_ROOM"))
    listen_port = _optional_int(
        getattr(dev, "listen_port", None),
        params.get("listen_port"),
        params.get("port"),
        env.get("DOT2_LISTEN_PORT"),
        env.get("DOT2_TLS_PORT"),
        plug_manifest.get(manifest, "transport", "port"),
        DEFAULT_TLS_PORT,
    )
    token_env = _text(
        getattr(dev, "token_env", None),
        params.get("token_env"),
        env.get("DOT2_TOKEN_ENV"),
        DEFAULT_TOKEN_ENV,
    )
    plug_id = _text(params.get("plug_id"), env.get("DOT2_PLUG_ID"))
    # C-001 §1/§2: DeviceConfig field > params > DOT2_SECRETS_DIR > a per-device
    # directory (when the deployment created one) > the shared "secrets". The
    # per-device default is what stops two planes from silently sharing one
    # device_token/TLS pair; an existing single-device layout is untouched.
    secrets_dir = _text(
        getattr(dev, "secrets_dir", None),
        params.get("secrets_dir"),
        env.get("DOT2_SECRETS_DIR"),
    )
    if not secrets_dir:
        per_device = Path("secrets") / device_id
        secrets_dir = str(per_device) if per_device.is_dir() else "secrets"
    # The EchoMuse X-EM-Token and rui's own transport admission are separate
    # secrets, so token_env names the adapter's own variable (manifest default:
    # DOT2_EM_TOKEN). Env wins; the deployment's secrets dir carries the same
    # token in <secrets_dir>/device_token (0600) so it need not be duplicated
    # into .env. Only the source is logged, never the value.
    token = env.get(token_env, "").strip() or env.get("DOT2_TOKEN", "").strip()
    token_source = "env" if token else ""
    if not token:
        token_file = Path(secrets_dir) / "device_token"
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            token_source = "file"
    token = token or None
    if token is None:
        log.warning(
            "dot2 adapter: no X-EM-Token in $%s or %s/device_token; "
            "the admission check is disabled",
            token_env,
            secrets_dir,
        )
    else:
        log.info("dot2 adapter: X-EM-Token source: %s", token_source)

    # -- device-side wake (opt-in; stock firmware has no model for our word) --
    oww_model = _text(params.get("oww_model"), env.get("DOT2_OWW_MODEL"))
    oww_on_device = _text(
        params.get("oww_on_device"),
        env.get("DOT2_OWW_ON_DEVICE"),
        "on" if oww_model else "off",
    )

    # -- audio / tuning -------------------------------------------------------
    mute = _flag(env.get("DOT2_MUTE"))
    aec_manifest = str(plug_manifest.get(manifest, "capabilities", "aec", default="device"))
    aec_enabled = _flag(env.get("DOT2_AEC_ENABLED"), aec_manifest != "none")
    mic_channels = _optional_int(
        params.get("mic_channels"),
        env.get("DOT2_MIC_CHANNELS"),
        plug_manifest.get(manifest, "audio", "capture", "mic_channels"),
        1,
    )
    ref_channels = _optional_int(
        params.get("ref_channels"),
        env.get("DOT2_REF_CHANNELS"),
        plug_manifest.get(manifest, "audio", "capture", "ref_channels"),
        0,
    )
    mic_gain_db = _optional_int(
        env.get("DOT2_MIC_GAIN_DB"),
        plug_manifest.get(manifest, "audio", "capture", "mic_gain_db"),
        DEFAULT_MIC_GAIN_DB,
    )
    cap_percent = _optional_float(
        params.get("volume_cap_percent"),
        env.get("DOT2_VOLUME_CAP"),
        plug_manifest.get(manifest, "volume", "ceiling_percent"),
        DEFAULT_VOLUME_CAP_PERCENT,
    )
    # Mute = the ceiling itself becomes 0, so every path (connect, reconciler,
    # physical buttons) lands on level 0 and the device stays silent while
    # wake/ASR/barge keep working. Flip DOT2_MUTE off and restart to unmute.
    volume_cap_percent = 0.0 if mute else float(cap_percent or 0.0)
    prebuffer_ms = _optional_int(
        env.get("DOT2_PREBUFFER_MS"),
        params.get("prebuffer_ms"),
        plug_manifest.get(manifest, "audio", "playback", "prebuffer_ms"),
        DEFAULT_PREBUFFER_MS,
    )
    prebuffer_start_ms = _optional_int(
        env.get("DOT2_PREBUFFER_START_MS"),
        params.get("prebuffer_start_ms"),
        plug_manifest.get(manifest, "audio", "playback", "prebuffer_start_ms"),
        DEFAULT_PREBUFFER_START_MS,
    )
    eos_idle_ms = _optional_int(
        env.get("DOT2_EOS_IDLE_MS"),
        params.get("eos_idle_ms"),
        plug_manifest.get(manifest, "audio", "playback", "eos_idle_ms"),
        DEFAULT_EOS_IDLE_MS,
    )
    inject_port = _optional_int(env.get("DOT2_INJECT_PORT"))
    # -- per-device runtime knobs (C-001 §2): params > DOT2_* env > default -----
    # Empty text means "unset": None goes into the ctor, which then owns the one
    # definition of the defaults (shared EQ dir; dev/rui/<device_id>/loudness.json;
    # control off; firmware wake threshold). control_port is resolved to an int
    # here because 0 is a real value ("disabled") that must not fall through to env.
    eq_dir = _text(params.get("eq_dir"), env.get("DOT2_EQ_DIR"))
    loudness_file = _text(params.get("loudness_file"), env.get("DOT2_LOUDNESS_FILE"))
    control_port = _optional_int(params.get("control_port"), env.get("DOT2_CONTROL_PORT"), 0)
    oww_threshold = _text(params.get("oww_threshold"), env.get("DOT2_OWW_THRESHOLD"))
    loudness = manifest.get("loudness")

    if plug_id or oww_model:
        log.info(
            "dot2 adapter: plug_id=%s oww_model=%s oww_on_device=%s",
            plug_id or "-",
            oww_model or "-",
            oww_on_device,
        )

    adapter = Dot2Adapter(
        device_id,
        host=host,
        room=room,
        secrets_dir=secrets_dir,
        tls_port=int(listen_port or DEFAULT_TLS_PORT),
        token=token,
        volume_cap_percent=volume_cap_percent,
        aec_enabled=aec_enabled,
        oww_on_device=oww_on_device,
        oww_model=oww_model,
        plug_id=plug_id,
        inject_port=inject_port,
        mic_channels=int(mic_channels or 1),
        ref_channels=int(ref_channels or 0),
        mic_gain_db=mic_gain_db,
        prebuffer_ms=prebuffer_ms,
        prebuffer_start_ms=prebuffer_start_ms,
        eos_idle_ms=eos_idle_ms,
        loudness_defaults=loudness if isinstance(loudness, dict) else None,
        eq_dir=eq_dir or None,
        loudness_file=loudness_file or None,
        control_port=control_port,
        oww_threshold=oww_threshold or None,
    )
    # Kit-only hints (not part of DeviceAdapter, ignored by the audio plane): this
    # is an inbound transport, so open() only starts listening and capture/playback
    # exist once the real EchoMuse client dials in. Without this the host-free kit
    # would fail on missing TLS material instead of recording an honest SKIP.
    adapter.kit_live_device_required = True  # type: ignore[attr-defined]
    adapter.kit_live_device_reason = (  # type: ignore[attr-defined]
        "inbound EchoMuse transport: open() listens for the Dot 2 on wss:8870 and "
        "capture/playback need the real client (and its TLS material) to dial in; "
        "probe(), the method surface and both drains were still exercised"
    )
    return adapter
