"""Loopback HTTP control surface for the loudness stage.

rui's backend does not share a process with the adapter, so tuning has to cross
a boundary. This is deliberately the smallest thing that works: JSON over HTTP
on 127.0.0.1, no framework, no auth requirement beyond the bind address.

    GET  /loudness        current configuration + what is loaded
    PUT  /loudness        partial update (JSON body) -> applies live, persists
    GET  /health          liveness + enabled state

    curl -s 127.0.0.1:8872/loudness
    curl -s -X PUT 127.0.0.1:8872/loudness -d '{"enabled":false}'
    curl -s -X PUT 127.0.0.1:8872/loudness -d '{"mode":"custom","trim_db":6,
                "bands":[{"type":"lowshelf","freq":150,"gain_db":6,"q":0.7}]}'

The endpoint is part of the plug: its manifest declares it, and the socket may
proxy it as a device-specific settings panel. Disable with DOT2_CONTROL_PORT=0.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .dsp import LoudnessProcessor

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024


class _HttpRequest:
    __slots__ = ("method", "path", "body")

    def __init__(self, method: str, path: str, body: bytes) -> None:
        self.method = method
        self.path = path
        self.body = body


async def _read_request(reader: asyncio.StreamReader) -> _HttpRequest | None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    lines = head.decode("latin-1").split("\r\n")
    if not lines:
        return None
    parts = lines[0].split()
    if len(parts) < 2:
        return None
    method, raw_path = parts[0].upper(), parts[1]
    path = raw_path.split("?", 1)[0]
    length = 0
    for line in lines[1:]:
        if line.lower().startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                length = 0
    body = b""
    if length:
        if length > MAX_BODY:
            return None
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
    return _HttpRequest(method, path, body)


async def _respond(writer: asyncio.StreamWriter, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
              413: "Payload Too Large"}.get(status, "Error")
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n").encode()
    writer.write(head + body)
    await writer.drain()
    writer.close()


def make_handler(processor: LoudnessProcessor, persist: Path | None):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await _read_request(reader)
        if request is None:
            await _respond(writer, 400, {"error": "bad request"})
            return
        try:
            if request.path == "/health" and request.method == "GET":
                await _respond(writer, 200, {"ok": True, "loudness": processor.describe()})
            elif request.path == "/loudness" and request.method == "GET":
                await _respond(writer, 200, processor.describe())
            elif request.path == "/loudness" and request.method == "PUT":
                try:
                    patch = json.loads(request.body or b"{}")
                    if not isinstance(patch, dict):
                        raise ValueError("body must be a JSON object")
                except (ValueError, UnicodeDecodeError) as exc:
                    await _respond(writer, 400, {"error": f"bad json: {exc}"})
                    return
                config = processor.update(patch)
                if persist is not None:
                    LoudnessProcessor.dump_config(persist, config)
                log.info("loudness updated: %s", json.dumps(processor.describe(), ensure_ascii=False))
                await _respond(writer, 200, processor.describe())
            else:
                await _respond(writer, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - a control request must never kill playback
            log.exception("loudness control failed")
            await _respond(writer, 400, {"error": f"{type(exc).__name__}: {exc}"})

    return handle


async def start_control_server(processor: LoudnessProcessor, host: str, port: int,
                               persist: Path | None = None) -> asyncio.Server:
    """Bind the loopback listener; ``OSError`` (e.g. EADDRINUSE) is the caller's.

    C-001 §4 makes port contention a per-device degradation, not a startup
    failure: adapter.open() catches this and runs the plane without the control
    surface. Keeping the low-level helper honest means a caller cannot mistake a
    failed bind for a listening socket.
    """
    server = await asyncio.start_server(make_handler(processor, persist), host, port)
    log.info("dot2 loudness control on http://%s:%d (GET/PUT /loudness)", host, port)
    return server
