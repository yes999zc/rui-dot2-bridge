"""Loopback-only test hook: play a WAV through the device while rui owns the link.

In production nothing writes here: the adapter only starts this server when
``DOT2_INJECT_PORT`` is set, and it binds 127.0.0.1. It exists because the only
speaker in the room is the one under test, and a wake word has to come from
somewhere for an unattended end-to-end run.

Protocol: one JSON line in, one JSON line out, per connection.

    {"wav": "/abs/path.wav"}          -> {"ok": true, "bytes": 153600}
    {"tone": 440, "seconds": 1.0}     -> generated in-process, same response

The WAV must be 48 kHz mono S16LE: the device's wire format. This hook does not
resample, on purpose - a test that silently changed the sample rate would not
test the path that runs in production.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from .frames import SPEAKER_RATE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .adapter import Dot2Adapter

log = logging.getLogger(__name__)


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        if (channels, width, rate) != (1, 2, SPEAKER_RATE):
            raise ValueError(
                f"{path}: expected mono 16-bit {SPEAKER_RATE} Hz, "
                f"got {channels} ch/{width * 8}-bit/{rate} Hz"
            )
        return wav.readframes(wav.getnframes())


def tone(seconds: float = 1.0, freq: float = 440.0, amplitude: float = 0.9) -> bytes:
    frames = int(SPEAKER_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / SPEAKER_RATE)))
        for i in range(frames)
    )


async def start_inject_server(adapter: "Dot2Adapter", host: str, port: int) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = json.loads(line or b"{}")
            if "wav" in request:
                pcm = read_wav(Path(str(request["wav"])))
            elif "tone" in request:
                pcm = tone(
                    float(request.get("seconds", 1.0)),
                    float(request.get("tone", 440.0)),
                    float(request.get("amplitude", 0.9)),
                )
            else:
                raise ValueError("expected 'wav' or 'tone'")
            await adapter.play(pcm, sample_rate=SPEAKER_RATE, channels=1)
            reply = {"ok": True, "bytes": len(pcm), "seconds": len(pcm) / (SPEAKER_RATE * 2)}
        except Exception as exc:  # noqa: BLE001 - a test hook reports, never crashes the plane
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, host, port)
    log.info("dot2 adapter: test injection listening on %s:%d", host, port)
    return server
