"""Run the Dot 2 link without rui: the P0 probe and the bench harness.

    .venv/bin/python -m rui_dot2_bridge.standalone --certs-dir secrets \
        --record /tmp/dot2-mic.wav --tone --duration 30

It accepts the device exactly like the adapter does (TLS, token, register/ack,
``mic_start``), prints one line every two seconds with the frame rate, level
and RTT, and can write the microphone to a WAV and play a test tone. Volume is
pinned to the ceiling (30 %) before any sound is produced.

This is a diagnostic tool, not the production path: in production rui's audio
plane owns the adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import struct
import time
import wave
from pathlib import Path

from .device_link import DeviceLink
from .frames import MIC_RATE, SPEAKER_CHUNK_BYTES, SPEAKER_RATE, mic_dbfs
from .volume import VOLUME_CAP_PERCENT, capped_level

log = logging.getLogger("rui_dot2_bridge.standalone")


def _tone(seconds: float = 1.6, freq: float = 440.0, amplitude: float = 0.06) -> bytes:
    """A quiet, unmistakable sine: enough to prove the speaker path, not to wake a house."""
    frames = int(SPEAKER_RATE * seconds)
    samples = bytearray()
    for i in range(frames):
        value = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / SPEAKER_RATE))
        samples += struct.pack("<h", value)
    return bytes(samples)


class WavWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(MIC_RATE)
        self.bytes_written = 0

    def write(self, pcm: bytes) -> None:
        self._wav.writeframes(pcm)
        self.bytes_written += len(pcm)

    def close(self) -> None:
        self._wav.close()


async def run(args: argparse.Namespace) -> dict:
    link = DeviceLink(
        certs_dir=Path(args.certs_dir),
        token=args.token,
        tls_port=args.tls_port,
        plain_port=args.plain_port,
        device_id=args.device_id,
        volume_cap_percent=args.volume_cap,
        push_config=not args.no_config,
    )
    await link.start()
    summary: dict = {"bound_ports": link.bound_ports, "volume_cap_percent": args.volume_cap}
    writer = WavWriter(Path(args.record)) if args.record else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def mic_loop() -> None:
        frames = 0
        since = time.monotonic()
        last_bytes = 0
        async for block in link.frames():
            if stop.is_set():
                return
            if writer is not None:
                writer.write(block)
            frames += 1
            now = time.monotonic()
            if now - since >= 2.0:
                rate = frames / (now - since)
                dbfs = mic_dbfs(block)
                log.info(
                    "mic: %.1f frames/s, %.1f KB/s, level %s dBFS, rtt %s ms, vol %d/%d",
                    rate,
                    (link.stats.mic_bytes - last_bytes) / 1024.0 / (now - since),
                    "silent" if dbfs == float("-inf") else f"{dbfs:.1f}",
                    link.stats.rtt_ms,
                    link.volume_level,
                    127,
                )
                frames = 0
                since = now
                last_bytes = link.stats.mic_bytes

    mic_task = asyncio.create_task(mic_loop(), name="standalone-mic")

    async def announce() -> None:
        ok = await link.wait_connected(timeout=args.connect_timeout)
        if not ok:
            log.error("no device connected within %.0f s", args.connect_timeout)
            summary["connected"] = False
            stop.set()
            return
        summary["connected"] = True
        summary["device"] = {
            "id": link.info.device_id,
            "version": link.info.version,
            "ip": link.info.ip,
            "capabilities": list(link.info.capabilities),
            "volume_level": link.volume_level,
        }
        log.info("device online: %s", json.dumps(summary["device"], ensure_ascii=False))
        if args.tone:
            await asyncio.sleep(0.5)
            level = capped_level(args.tone_percent, args.volume_cap)
            log.info("playing test tone at %.0f%% (level %d)", args.tone_percent, level)
            await link.send_pcm(_tone(args.tone_seconds))
            await asyncio.sleep(0.3)
            await link.end_stream()
            summary["tone_sent"] = True

    announce_task = asyncio.create_task(announce(), name="standalone-announce")

    deadline = None if args.duration <= 0 else time.monotonic() + args.duration
    while not stop.is_set():
        await asyncio.sleep(0.2)
        if deadline is not None and time.monotonic() >= deadline:
            break
        if announce_task.done() and not summary.get("connected"):
            break

    stop.set()
    for task in (announce_task, mic_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if writer is not None:
        writer.close()
        summary["record"] = {"path": str(writer.path), "bytes": writer.bytes_written}
    summary["stats"] = {
        "control_connects": link.stats.control_connects,
        "data_connects": link.stats.data_connects,
        "mic_frames": link.stats.mic_frames,
        "mic_bytes": link.stats.mic_bytes,
        "speaker_frames": link.stats.speaker_frames,
        "dropped_audio": link.stats.dropped_audio,
        "rtt_ms": link.stats.rtt_ms,
    }
    await link.stop()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certs-dir", default="secrets")
    parser.add_argument("--token", default=os.environ.get("DOT2_TOKEN"))
    parser.add_argument("--tls-port", type=int, default=8870)
    parser.add_argument("--plain-port", type=int, default=None)
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--volume-cap", type=float, default=VOLUME_CAP_PERCENT)
    parser.add_argument("--tone", action="store_true", help="play a 440 Hz test tone once")
    parser.add_argument("--tone-seconds", type=float, default=1.6, help="length of the test tone")
    parser.add_argument("--tone-percent", type=float, default=VOLUME_CAP_PERCENT)
    parser.add_argument("--record", default=None, help="write the microphone to this WAV")
    parser.add_argument("--duration", type=float, default=30.0, help="0 = run until Ctrl-C")
    parser.add_argument("--connect-timeout", type=float, default=120.0)
    parser.add_argument("--no-config", action="store_true", help="do not push a config message")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    summary = asyncio.run(run(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("connected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
