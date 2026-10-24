"""The EchoMuse device link: a controller the Dot 2 can dial.

This speaks the subset of ``docs/device-controller-interface.md`` that a voice
satellite needs (see ``docs/protocol-map.md``):

* two websockets, ``/control`` (JSON) and ``/data`` (binary), with the device's
  ``X-EM-Token`` header when it has a token installed;
* ``register`` -> ``ack``, then ``mic_start(lock_mic=false)`` for the
  continuous 80 ms mono stream;
* uplink ``0x01`` frames are queued for the audio plane; downlink ``0x02``
  frames carry 48 kHz mono PCM and ``0x03`` ends a stream.

It is a server: the device dials us (mDNS advertisement or the static
``/data/local/etc/echomuse/controller.json`` list). One device at a time, the
same rule the firmware follows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .frames import (
    FRAME_MIC,
    FRAME_NO_SPEECH_TIMEOUT,
    FRAME_VAD_END,
    speaker_eos,
    split_playback,
)
from .volume import DEVICE_LEVEL_MAX, VOLUME_CAP_PERCENT, capped_level

log = logging.getLogger(__name__)

CONTROL_PATH = "/control"
DATA_PATH = "/data"
PING_INTERVAL_S = 15.0
HANDSHAKE_TIMEOUT_S = 10.0
AUDIO_QUEUE_CHUNKS = 64          # 64 x 80 ms ≈ 5 s of slack before dropping oldest
CONFIG_CACHE = "device-log.jsonl"


@dataclass
class LinkStats:
    control_connects: int = 0
    data_connects: int = 0
    mic_frames: int = 0
    mic_bytes: int = 0
    speaker_frames: int = 0
    dropped_audio: int = 0
    rtt_ms: float | None = None
    last_mic_at: float = 0.0


@dataclass
class DeviceInfoReport:
    """What the device said in ``register`` (unknown keys preserved)."""

    device_id: str = ""
    version: str = ""
    capabilities: tuple[str, ...] = ()
    ip: str = ""
    base_os: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class DeviceLink:
    """Accept one Echo Dot 2 and turn it into a pair of byte streams."""

    def __init__(
        self,
        *,
        certs_dir: str | Path | None = None,
        token: str | None = None,
        tls_port: int | None = 8870,
        plain_port: int | None = None,
        bind_host: str = "0.0.0.0",
        device_id: str | None = None,
        volume_cap_percent: float = VOLUME_CAP_PERCENT,
        push_config: bool = True,
        aec_enabled: bool = True,
        mic_gain_db: int | None = None,
        oww_on_device: str = "off",
        on_button: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.certs_dir = Path(certs_dir) if certs_dir else None
        self.token = token
        self.tls_port = tls_port
        self.plain_port = plain_port
        self.bind_host = bind_host
        self.device_id = device_id
        self.volume_cap_percent = max(0.0, min(float(volume_cap_percent), 100.0))
        self.push_config = push_config
        self.aec_enabled = aec_enabled
        # Fixed mic pre-gain (EchoMuse clampMicGainDb: 0..42). The device defaults
        # to +24 dB, which clips the Dot 2's mics on normal speech (peak 1.0000);
        # the controller can lower it in the config push.
        self.mic_gain_db = mic_gain_db
        self.oww_on_device = oww_on_device
        self.on_button = on_button

        self.stats = LinkStats()
        self.info = DeviceInfoReport()
        self.audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=AUDIO_QUEUE_CHUNKS)
        self.connected = asyncio.Event()

        self._control: ServerConnection | None = None
        self._data: ServerConnection | None = None
        self._servers: list[Server] = []
        self._send_lock = asyncio.Lock()
        self._ping_task: asyncio.Task[None] | None = None
        self._ping_seq = 0
        self._pong_waiters: dict[int, float] = {}
        # Target level (ours), not "whatever the device last reported". The two were
        # one variable, and a stale volume_state from before the connect push (the
        # device reports its old 38 after we send 89) overwrote the target and then
        # "pulled back" to it - the morning "speaker is very quiet" bug.
        self._volume_level = capped_level(100.0, self.volume_cap_percent)
        self._reported_level: int | None = None
        self._device_log: deque[str] = deque(maxlen=200)
        self._closing = False
        self.bound_ports: dict[str, int] = {}

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._servers:
            return
        if self.tls_port:
            context = self._tls_context()
            server = await serve(
                self._handle,
                self.bind_host,
                self.tls_port,
                ssl=context,
                max_size=1 << 20,
                ping_interval=20,
                ping_timeout=40,
            )
            self._servers.append(server)
            self.bound_ports["tls"] = self._port_of(server)
            log.info("dot2 link: wss listening on %s", self.bound_ports["tls"])
        if self.plain_port is not None:
            server = await serve(
                self._handle,
                self.bind_host,
                self.plain_port,
                max_size=1 << 20,
                ping_interval=20,
                ping_timeout=40,
            )
            self._servers.append(server)
            self.bound_ports["plain"] = self._port_of(server)
            log.info("dot2 link: ws listening on %s", self.bound_ports["plain"])

    async def stop(self) -> None:
        self._closing = True
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        for conn in (self._control, self._data):
            if conn is not None:
                await _quiet_close(conn)
        self._control = None
        self._data = None
        for server in self._servers:
            server.close()
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
        self.connected.clear()

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # ------------------------------------------------------------- transport
    def _tls_context(self) -> ssl.SSLContext:
        if self.certs_dir is None:
            raise RuntimeError("tls_port requires certs_dir with server.pem/server.key")
        cert = self.certs_dir / "server.pem"
        key = self.certs_dir / "server.key"
        if not cert.is_file() or not key.is_file():
            raise RuntimeError(f"missing TLS material: {cert} / {key}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        return context

    @staticmethod
    def _port_of(server: Server) -> int:
        sockets = getattr(server, "sockets", None) or []
        return sockets[0].getsockname()[1] if sockets else 0

    def _authorized(self, conn: ServerConnection) -> bool:
        if not self.token:
            return True
        return conn.request.headers.get("X-EM-Token") == self.token

    async def _handle(self, conn: ServerConnection) -> None:
        path = conn.request.path.split("?", 1)[0]
        if not self._authorized(conn):
            log.warning("dot2 link: refused %s from %s (token mismatch)", path, conn.remote_address)
            await _send_json(conn, {"type": "refused"})
            await _quiet_close(conn, 1008, "token")
            return
        if path == CONTROL_PATH:
            await self._serve_control(conn)
        elif path == DATA_PATH:
            await self._serve_data(conn)
        else:
            log.warning("dot2 link: unknown path %s", path)
            await _quiet_close(conn, 1008, "unknown path")

    # --------------------------------------------------------------- control
    async def _serve_control(self, conn: ServerConnection) -> None:
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=HANDSHAKE_TIMEOUT_S)
        except (TimeoutError, ConnectionClosed):
            await _quiet_close(conn)
            return
        try:
            message = json.loads(raw if isinstance(raw, str) else raw.decode())
        except (ValueError, UnicodeDecodeError):
            await _quiet_close(conn, 1002, "bad register")
            return
        if message.get("type") != "register":
            log.warning("dot2 link: first message was %r, expected register", message.get("type"))
            await _quiet_close(conn, 1002, "expected register")
            return

        self.info = DeviceInfoReport(
            device_id=str(message.get("device_id") or ""),
            version=str(message.get("version") or ""),
            capabilities=tuple(message.get("capabilities") or ()),
            ip=str(message.get("ip") or ""),
            base_os=str(message.get("base_os") or ""),
            raw=message,
        )
        self.device_id = self.device_id or self.info.device_id
        self._control = conn
        self.stats.control_connects += 1

        await self._send_control(
            {
                "type": "ack",
                "device_id": self.device_id,
                "features": list(self.controller_features),
                "time_ms": int(time.time() * 1000),
            }
        )
        self.connected.set()
        log.info(
            "dot2 link: %s registered (fw %s, %s, ip %s, caps %s)",
            self.device_id,
            self.info.version,
            self.info.base_os or "unknown os",
            self.info.ip or "?",
            ",".join(self.info.capabilities),
        )

        # Quiet by construction: the ceiling is applied before anything plays.
        await self.set_volume_percent(self.volume_cap_percent)
        if self.push_config:
            await self._send_control(
                {
                    "type": "config",
                    "startupVolume": self._volume_level,
                    "owwOnDevice": self.oww_on_device,
                    "wakeWordEnabled": False,
                    "aecEnabled": self.aec_enabled,
                    **({"micGainDb": self.mic_gain_db} if self.mic_gain_db is not None else {}),
                }
            )
        await self._send_control({"type": "mic_start", "lock_mic": False})

        self._ping_task = asyncio.create_task(self._ping_loop(), name="dot2-ping")
        try:
            async for raw in conn:
                await self._on_control_message(raw)
        except ConnectionClosed:
            pass
        finally:
            if self._ping_task is not None:
                self._ping_task.cancel()
                self._ping_task = None
            if self._control is conn:
                self._control = None
                self.connected.clear()
            log.info("dot2 link: control closed (%s)", self.device_id)

    async def _on_control_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            return
        try:
            message = json.loads(raw)
        except ValueError:
            return
        kind = message.get("type")
        if kind == "pong":
            self._on_pong(message)
        elif kind == "volume_state":
            level = int(message.get("level") or 0)
            self._reported_level = level
            if level > self._volume_level:
                # The ceiling wins over the physical buttons. A report BELOW the
                # target is the device confirming an older value; it must never
                # lower the target, only be recorded.
                log.info("dot2 link: volume %d above ceiling, pulling back to %d", level, self._volume_level)
                await self.set_volume_level(self._volume_level)
        elif kind == "button":
            log.info("dot2 link: button %s", message)
            if self.on_button is not None:
                try:
                    await self.on_button(message)
                except Exception:  # noqa: BLE001 - a handler bug must not kill the link
                    log.exception("dot2 link: on_button handler failed")
        elif kind == "stats":
            log.debug("dot2 link: stats %s", message)
        elif kind == "log":
            line = f"[device {message.get('level', '?')}] {message.get('message', '')}"
            self._device_log.append(line)
            log.debug(line)
        elif kind == "listen_state":
            log.info("dot2 link: listen_state=%s", message.get("state"))
        else:
            log.debug("dot2 link: control %s", kind)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            self._ping_seq += 1
            ping_id = self._ping_seq
            self._pong_waiters[ping_id] = time.monotonic()
            try:
                await self._send_control({"type": "ping", "id": ping_id})
            except ConnectionClosed:
                return

    def _on_pong(self, message: dict[str, Any]) -> None:
        ping_id = message.get("id")
        if not isinstance(ping_id, int):
            return
        sent_at = self._pong_waiters.pop(ping_id, None)
        if sent_at is not None:
            self.stats.rtt_ms = round((time.monotonic() - sent_at) * 1000.0, 1)

    # ------------------------------------------------------------------ data
    async def _serve_data(self, conn: ServerConnection) -> None:
        self._data = conn
        self.stats.data_connects += 1
        log.info("dot2 link: data plane connected (%s)", self.device_id)
        try:
            async for raw in conn:
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    continue
                kind = raw[0]
                payload = bytes(raw[1:])
                if kind == FRAME_MIC:
                    self._enqueue_audio(payload)
                elif kind in (FRAME_VAD_END, FRAME_NO_SPEECH_TIMEOUT):
                    log.debug("dot2 link: turn sentinel 0x%02x", kind)
                else:
                    log.debug("dot2 link: data frame 0x%02x (%d B)", kind, len(payload))
        except ConnectionClosed:
            pass
        finally:
            if self._data is conn:
                self._data = None
            log.info("dot2 link: data plane closed (%s)", self.device_id)

    def _enqueue_audio(self, payload: bytes) -> None:
        self.stats.mic_frames += 1
        self.stats.mic_bytes += len(payload)
        self.stats.last_mic_at = time.monotonic()
        try:
            self.audio.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                self.audio.get_nowait()
                self.stats.dropped_audio += 1
            except asyncio.QueueEmpty:
                pass
            self.audio.put_nowait(payload)

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield the device's 80 ms mono blocks until the link is closed."""
        while not self._closing:
            try:
                yield await asyncio.wait_for(self.audio.get(), timeout=0.5)
            except TimeoutError:
                continue

    # -------------------------------------------------------------- commands
    @property
    def controller_features(self) -> tuple[str, ...]:
        """What this controller can do. Empty until a feature is actually implemented."""
        return ()

    async def _send_control(self, message: dict[str, Any]) -> None:
        conn = self._control
        if conn is None:
            raise RuntimeError("device control plane is not connected")
        text = json.dumps(message, separators=(",", ":"))
        async with self._send_lock:
            await conn.send(text)

    async def send_pcm(self, pcm: bytes) -> None:
        """Queue 48 kHz mono PCM as ``0x02`` frames (whole frames only)."""
        conn = self._data
        if conn is None:
            raise RuntimeError("device data plane is not connected")
        for frame in split_playback(pcm):
            async with self._send_lock:
                await conn.send(frame)
            self.stats.speaker_frames += 1

    async def end_stream(self) -> None:
        conn = self._data
        if conn is None:
            return
        async with self._send_lock:
            await conn.send(speaker_eos())

    async def flush(self) -> None:
        """Discard audio the device has already buffered (barge-in)."""
        await self._send_control({"type": "speaker_flush"})

    async def set_volume_level(self, level: int) -> None:
        self._volume_level = max(0, min(DEVICE_LEVEL_MAX, int(level)))
        await self._send_control({"type": "volume_set", "level": self._volume_level})

    async def set_volume_percent(self, percent: float) -> None:
        await self.set_volume_level(capped_level(percent, self.volume_cap_percent))

    @property
    def volume_level(self) -> int:
        return self._volume_level

    @property
    def reported_level(self) -> int | None:
        """The device's own last report (may lag the target by one message)."""
        return self._reported_level

    async def set_leds(self, leds: list[dict[str, Any]], listening: bool = False) -> None:
        await self._send_control({"type": "leds", "leds": leds, "listening": listening})


async def _send_json(conn: ServerConnection, payload: dict[str, Any]) -> None:
    try:
        await conn.send(json.dumps(payload, separators=(",", ":")))
    except ConnectionClosed:
        pass


async def _quiet_close(conn: ServerConnection, code: int = 1000, reason: str = "") -> None:
    try:
        await conn.close(code, reason)
    except Exception:  # noqa: BLE001 - closing must never raise into the caller
        pass
