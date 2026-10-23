"""Fake EchoMuse device: drives the adapter without the Dot, for tests and CI.

It speaks the same subset our adapter accepts (register -> ack -> /data), and can
send either mono or ``[mic | reference]`` 2-channel frames. That is how the P3
two-channel path is verified before any firmware is flashed: rui must build its
AEC, accept the frames, and report ``aec_enabled``/``ref_active``.

    python tools/fake_device.py --url wss://127.0.0.1:8890 --channels 2 --seconds 15

Not a substitute for the real device: no TLS verification (a test client), no
register fields beyond what the adapter reads, and it makes no audio.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import ssl
import struct
import time

import websockets.asyncio.client as ws_client
from websockets.asyncio.client import ClientConnection


def frame(channels: int, samples: int, n: int) -> bytes:
    """One 0x01 chunk: mono, or interleaved [mic | ref]."""
    mic = []
    for i in range(samples):
        t = (n * samples + i) / 16000.0
        voice = 0.05 * math.sin(2 * math.pi * 220 * t) + 0.002 * ((i * 7919) % 1024 - 512) / 512
        mic.append(int(voice * 32767))
    if channels == 1:
        return bytes([0x01]) + struct.pack(f"<{samples}h", *mic)
    ref = [int(0.2 * 32767 * math.sin(2 * math.pi * 440 * t)) for t in
           ((n * samples + i) / 16000.0 for i in range(samples))]
    interleaved = []
    for m, r in zip(mic, ref):
        interleaved.extend((m, r))
    return bytes([0x01]) + struct.pack(f"<{samples * 2}h", *interleaved)


async def read_json(ws: ClientConnection, timeout: float = 3.0) -> dict | None:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout)
    except (TimeoutError, Exception):
        return None
    if isinstance(raw, bytes):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def run(args: argparse.Namespace) -> int:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE  # test client: the device's pinning is what matters
    headers = {"X-EM-Token": args.token} if args.token else {}
    base = args.url.rstrip("/")

    async with ws_client.connect(
        f"{base}/control", additional_headers=headers, ssl=ssl_context, max_size=1 << 20
    ) as control:
        await control.send(json.dumps({
            "type": "register",
            "device_id": args.device_id,
            "version": "fake-v0",
            "capabilities": ["mic", "speaker", "buttons", "aec_hw_ref", "ref_uplink"],
            "ip": "127.0.0.1",
            "base_os": "fireos",
        }))
        ack = await read_json(control)
        if not ack or ack.get("type") != "ack":
            print(f"no ack: {ack}")
            return 1
        print("ack:", json.dumps(ack))

        async def control_reader() -> None:
            while True:
                message = await read_json(control, 60)
                if message is None:
                    continue
                if message.get("type") == "ping":
                    await control.send(json.dumps({"type": "pong", "id": message.get("id")}))

        reader_task = asyncio.create_task(control_reader(), name="fake-control")

        async with ws_client.connect(
            f"{base}/data", additional_headers=headers, ssl=ssl_context, max_size=1 << 20
        ) as data:
            print(f"sending {args.channels}ch frames for {args.seconds:.0f}s")
            samples = 1280  # 80 ms at 16 kHz
            period = 0.08
            started = time.monotonic()
            n = 0
            while time.monotonic() - started < args.seconds:
                await data.send(frame(args.channels, samples, n))
                n += 1
                await asyncio.sleep(period)
            print(f"sent {n} frames ({n * period:.1f}s of audio)")

        reader_task.cancel()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="wss://127.0.0.1:8890")
    parser.add_argument("--token", default=None)
    parser.add_argument("--channels", type=int, default=1, choices=(1, 2))
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--device-id", default="FAKEDOT2")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
