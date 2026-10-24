"""EchoMuse device wire format: frame types, chunking, and the rui AudioFrame bridge.

Wire facts (see docs/findings.md for the source of each):

* ``/data`` frames start with one type byte, one namespaced by direction; mic
  audio frames are ``[0x01][seq: u16 big-endian][payload]`` (upstream
  ``sendFrame`` in data.go). The sequence number is NOT audio — treating it as
  PCM shifts the signal by one sample per block, harmless for mono ASR but fatal
  for a 2ch mic+reference uplink (channels end up swapped/offset).
* capture (device -> us):  ``0x01`` mic PCM, ``S16LE mono 16 kHz``, 80 ms per
  chunk; ``payload`` is 2560 bytes mono or 5120 bytes for the 2ch
  ``[mic | ref]`` mode (feature ``dot2_hw_ref``).
* playback (us -> device): ``0x02`` speaker PCM, ``S16LE mono 48 kHz``, 4096 B
  (42.7 ms) per chunk; ``0x03`` end of stream.
* the device's raw capture is 9-channel 24-bit; it never leaves the device
  (firmware does beamforming/AEC/AGC and sends mono). Phase 3 may change that.

Nothing here touches sockets: it is pure byte/format handling so it can be unit
tested without hardware.
"""

from __future__ import annotations

# Capture: device -> controller.
FRAME_MIC = 0x01
FRAME_VAD_END = 0x04          # bounded turn: speech detected, then ended
FRAME_NO_SPEECH_TIMEOUT = 0x05

# Playback: controller -> device.
FRAME_SPEAKER = 0x02
FRAME_SPEAKER_EOS = 0x03
FRAME_MUSIC = 0x04            # only for devices announcing audio_mix
FRAME_MUSIC_EOS = 0x05

MIC_RATE = 16000
MIC_CHUNK_BYTES = 1280 * 2    # 80 ms, the device's own batching (vadOwwChunkBytes)
VOICE_FRAME_MS = 20           # what rui's plane is tuned for
SPEAKER_RATE = 48000
SPEAKER_CHUNK_BYTES = 4096    # 42.7 ms mono S16, the controller's period


#: Device -> controller control frames ride the mic frame type with a one-byte
#: payload: the upstream writer has no separate type for them.
MIC_CONTROL_END = 0x04          # bounded turn: speech detected, then ended
MIC_CONTROL_NO_SPEECH = 0x05    # no speech within the turn timeout


def parse_data_frame(frame: bytes) -> tuple[int, bytes]:
    """Split one ``/data`` websocket message into ``(type, payload)``.

    Mic frames carry a big-endian u16 sequence number after the type byte; it is
    stripped here so callers only ever see PCM. Unknown types are returned as-is
    rather than raised: the protocol requires both ends to ignore what they do
    not understand.
    """
    if not frame:
        raise ValueError("empty /data frame")
    kind = frame[0]
    if kind == FRAME_MIC:
        if len(frame) < 3:
            return kind, b""
        return kind, frame[3:]
    return kind, frame[1:]


def mic_control(payload: bytes) -> int | None:
    """Return the control byte for a one-byte mic payload, else None."""
    if len(payload) == 1 and payload[0] in (MIC_CONTROL_END, MIC_CONTROL_NO_SPEECH):
        return payload[0]
    return None


def speaker_frame(pcm: bytes) -> bytes:
    """Wrap up to ``SPEAKER_CHUNK_BYTES`` of 48 kHz mono S16 as a ``0x02`` frame."""
    if len(pcm) > SPEAKER_CHUNK_BYTES:
        raise ValueError(f"speaker chunk is {len(pcm)} bytes, max {SPEAKER_CHUNK_BYTES}")
    return bytes([FRAME_SPEAKER]) + pcm


def speaker_eos() -> bytes:
    return bytes([FRAME_SPEAKER_EOS])


def split_playback(pcm: bytes) -> list[bytes]:
    """Cut our PCM into wire-sized ``0x02`` frames.

    The device primes ~1.1 s of buffer before it starts; sending whole frames
    keeps its own pacing intact, so the adapter does not invent a clock.
    """
    return [speaker_frame(pcm[i : i + SPEAKER_CHUNK_BYTES])
            for i in range(0, len(pcm), SPEAKER_CHUNK_BYTES)]


def rechunk_voice(pcm: bytes, *, frame_ms: int = VOICE_FRAME_MS) -> list[bytes]:
    """Cut the device's 80 ms blocks into rui-sized 20 ms blocks.

    S16LE mono at 16 kHz: 32 bytes per millisecond. A partial tail is kept (the
    next device chunk continues it) — callers that need whole frames should
    buffer; this helper returns whatever the input allows.
    """
    step = MIC_RATE * 2 * frame_ms // 1000
    if step <= 0:
        raise ValueError("frame_ms too small")
    return [pcm[i : i + step] for i in range(0, len(pcm), step)]


def mic_dbfs(pcm: bytes) -> float:
    """RMS of one S16LE block in dBFS; ``-inf`` for silence. Used by metrics."""
    import math
    import struct

    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return float("-inf")
    mean_sq = sum(s * s for s in samples) / len(samples)
    if mean_sq <= 0:
        return float("-inf")
    return 10.0 * math.log10(mean_sq / (32768.0 * 32768.0))
