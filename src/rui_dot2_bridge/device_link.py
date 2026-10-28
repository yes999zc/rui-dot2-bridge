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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .frames import (
    FRAME_MIC,
    FRAME_NO_SPEECH_TIMEOUT,
    FRAME_VAD_END,
    mic_control,
    speaker_eos,
    split_playback,
)
from .volume import DEVICE_LEVEL_MAX, VOLUME_CAP_PERCENT, capped_level
from .wake import MAX_PENDING_WAKES, BoundedWakeQueue, map_oww_wake

log = logging.getLogger(__name__)

CONTROL_PATH = "/control"
DATA_PATH = "/data"
PING_INTERVAL_S = 15.0
HANDSHAKE_TIMEOUT_S = 10.0
AUDIO_QUEUE_CHUNKS = 64          # 64 x 80 ms ≈ 5 s of slack before dropping oldest
CONFIG_CACHE = "device-log.jsonl"

# P0b: one /data frame is 4096 B = 42.7 ms of 48 kHz mono S16. A healthy
# localhost/LAN write returns in well under a millisecond; 0.75 s means ~17
# frames of audio could not even be handed to the kernel, i.e. the device
# stopped reading and the socket buffer is full. websockets' send() waits for
# the transport to drain, so without this bound the ping write parks behind it
# and ping_timeout never even starts (the 887 s zombie).
WRITE_TIMEOUT_S = 0.75
# A refusal (bad register) must reach the device, but the peer might already be
# gone: never let the error close hold the handler for websockets' 10 s default.
CLOSE_TIMEOUT_S = 1.0
# A pong for a connection the device abandoned can never arrive; a waiter older
# than this is forgotten instead of accumulating for the life of the process.
PONG_WAITER_TTL_S = 60.0
# A control frame is tiny: if it cannot even be handed to the kernel in 3 s the
# peer stopped reading long ago. The bound matters beyond the write itself - the
# next register's ack waits on this same lock, so an unbounded send is a stale
# queue that can hold a reconnect hostage.
CONTROL_WRITE_TIMEOUT_S = 3.0
# P0b-C1: websockets' keepalive cannot see a half-open socket: ping() awaits the
# write and only then starts ping_timeout, so a full send buffer parks the probe
# forever (the 887 s zombie). This is the independent backstop: a link on which
# NO plane delivered a control frame, a pong or a /data frame for this long is
# gone, and dropping it lets the device dial again (registering is the only way
# back to a usable control plane). 45 s = three missed 15 s pings; the mic stream
# normally delivers a frame every 80 ms, so an idle room never reaches it.
HALF_OPEN_TIMEOUT_S = 45.0
# Check a few times a second is pointless; 5 s bounds the detection lag to one
# interval while adding one wakeup per 5 s to an otherwise idle bridge.
WATCHDOG_INTERVAL_S = 5.0
# websockets' own keepalive, made explicit: a ping is a write, so it can only be
# a supplement to the watchdog, never the mechanism. write_limit is the
# high-water mark of the per-connection write buffer (~0.68 s of 48 kHz mono);
# beyond it send() applies backpressure, and the bounded writes above then abort
# the stalled plane instead of buffering seconds of audio.
KEEPALIVE_PING_INTERVAL_S = 20.0
KEEPALIVE_PING_TIMEOUT_S = 40.0
WRITE_LIMIT_BYTES = 32 * 1024
# The link has no outbound queue; 256 KiB is 2.73 s of 48 kHz mono S16, the
# most PCM we are willing to have accepted-but-unwritten across concurrent
# send_pcm calls. Beyond that the device is gone: abort and let it reconnect
# rather than buffering seconds of audio that could replay after a reconnect.
SEND_WATERMARK_BYTES = 256 * 1024


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
    # Last /data frame actually written to the device (monotonic). Together with
    # last_mic_at this is the adapter's proof that the data plane is alive.
    last_pcm_sent_at: float = 0.0
    half_open_drops: int = 0
    wake_events: int = 0
    invalid_wakes: int = 0
    shadow_crossings: int = 0


@dataclass
class DeviceInfoReport:
    """What the device said in ``register`` (unknown keys preserved)."""

    device_id: str = ""
    version: str = ""
    capabilities: tuple[str, ...] = ()
    ip: str = ""
    base_os: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ConnMeter:
    """Per-connection counters and tasks, so a close line can be honest.

    Keyed by ``id(conn)`` while that connection's handler runs. A reconnect that
    lands before the bridge notices the old socket died gives two incarnations
    of the same plane, and each keeps its own counter and ping task: neither may
    borrow (or tear down) the other's.
    """

    direction: str
    peer: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    # Last inbound frame on THIS socket (monotonic). The half-open watchdog reads
    # only this: a successful write proves the kernel accepted bytes, not that
    # the device read them.
    last_rx_at: float = field(default_factory=time.monotonic)
    ping_task: asyncio.Task[None] | None = None
    # True once the post-mortem for this connection was already logged (a
    # refusal names its reason), so _drop_control does not say it twice.
    accounted: bool = False


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
        ref_uplink: bool = False,
        mic_gain_db: int | None = None,
        aec_tail_ms: int | None = None,
        aec_delay_ms: int | None = None,
        agc_enabled: bool | None = None,
        barge_in_enabled: bool | None = None,
        barge_in_threshold: float | None = None,
        wake_mic: int | None = None,
        aec_ref_source: str | None = None,
        oww_on_device: str | None = "off",
        oww_model: str | None = None,
        oww_threshold: float | None = None,
        playing: Callable[[], bool] | None = None,
        wake_queue: BoundedWakeQueue | None = None,
        wake_word_enabled: bool | None = None,
        on_button: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_link_lost: Callable[[str], None] | None = None,
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
        # Advertise dot2_hw_ref so a patched firmware (v2.19.0-ea.1 + the
        # echomuse-dot2-ref patch) sends [beam | de-aliased ch8 ref] instead of
        # mono. The stock firmware ignores the feature, so this is safe before
        # the patch is installed.
        self.ref_uplink = ref_uplink
        # Fixed mic pre-gain (EchoMuse clampMicGainDb: 0..42). The device defaults
        # to +24 dB, which clips the Dot 2's mics on normal speech (peak 1.0000);
        # the controller can lower it in the config push.
        self.mic_gain_db = mic_gain_db
        # Optional device-side DSP overrides. EchoMuse accepts all of these in the
        # config push; leaving one None keeps the firmware default.
        self.aec_tail_ms = aec_tail_ms
        self.aec_delay_ms = aec_delay_ms
        self.agc_enabled = agc_enabled
        self.barge_in_enabled = barge_in_enabled
        self.barge_in_threshold = barge_in_threshold
        # wakeMic picks which microphone feeds the device gate (0 centre, the
        # periphery has a different echo path); aecRefSource selects hw/sw ref.
        self.wake_mic = wake_mic
        self.aec_ref_source = aec_ref_source
        self.oww_on_device = oww_on_device
        # Which OWW classifier the device should score with, and the bar it must
        # clear. The firmware defaults to hey_jarvis_v0.1 / 0.5 when the push omits
        # them, but the deployment that installed a model has to name it here: the
        # device only opens the scorer for the model in this config.
        self.oww_model = str(oww_model or "").strip()
        self.oww_threshold = oww_threshold
        # The adapter owns playback state, the link does not; a device-detected wake
        # that lands while our own reply is playing is a barge-in by definition (the
        # stream-mode firmware cannot flag it - it only sets `barge` inside a private
        # listen session, which needs a controller that implements sessions).
        self.playing = playing
        # One bounded, thread-safe queue per device: the control socket is the
        # producer and rui's audio plane is the single drainer (v1 section 4.2).
        # An adapter may pass its own queue so wake injection works before open().
        self.wakes = wake_queue if wake_queue is not None else BoundedWakeQueue(
            MAX_PENDING_WAKES
        )
        self.wake_word_enabled = (
            bool(wake_word_enabled)
            if wake_word_enabled is not None
            else self.oww_on_device == "on"
        )
        self._listen_state = ""
        # True once the device has proved it actually scored (oww_wake, or an
        # oww_shadow_cross). This is the second honest proof of on-device wake,
        # for the streaming firmware path where listen_state stays "stream"
        # because the controller does not implement private-listen sessions.
        self._scoring_evidence = False
        self.on_button = on_button
        # A sync callback for "the device link just went away". The adapter uses
        # it to drop its half-sentence buffer: the link has no outbound PCM queue,
        # so a reconnect must not drain bytes the previous connection never saw.
        self.on_link_lost = on_link_lost

        self.stats = LinkStats()
        self.info = DeviceInfoReport()
        self.audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=AUDIO_QUEUE_CHUNKS)
        self.connected = asyncio.Event()

        self._control: ServerConnection | None = None
        self._data: ServerConnection | None = None
        self._servers: list[Server] = []
        # Control and data are independent sockets. Sharing one lock let a
        # stalled /data write park volume_set/ping/flush, and because the ping
        # never reached the wire its ping_timeout never started either.
        self._control_lock = asyncio.Lock()
        self._data_lock = asyncio.Lock()
        # Bytes accepted into send_pcm but not yet written to /data. Guarded by
        # the single event loop (no await between read and update).
        self._inflight_bytes = 0
        self._ping_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._ping_seq = 0
        self._pong_waiters: dict[int, float] = {}
        self._pong_events: dict[int, asyncio.Event] = {}
        # Target level (ours), not "whatever the device last reported". The two were
        # one variable, and a stale volume_state from before the connect push (the
        # device reports its old 38 after we send 89) overwrote the target and then
        # "pulled back" to it - the morning "speaker is very quiet" bug.
        self._volume_level = capped_level(100.0, self.volume_cap_percent)
        self._reported_level: int | None = None
        self._device_log: deque[str] = deque(maxlen=200)
        self._closing = False
        self.bound_ports: dict[str, int] = {}
        # Per-connection bookkeeping (byte counters + the ping task it owns),
        # keyed by id(conn) and dropped by that connection's own handler.
        self._meters: dict[int, _ConnMeter] = {}

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
                ping_interval=KEEPALIVE_PING_INTERVAL_S,
                ping_timeout=KEEPALIVE_PING_TIMEOUT_S,
                write_limit=WRITE_LIMIT_BYTES,
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
                ping_interval=KEEPALIVE_PING_INTERVAL_S,
                ping_timeout=KEEPALIVE_PING_TIMEOUT_S,
                write_limit=WRITE_LIMIT_BYTES,
            )
            self._servers.append(server)
            self.bound_ports["plain"] = self._port_of(server)
            log.info("dot2 link: ws listening on %s", self.bound_ports["plain"])
        # One watchdog for the whole link, independent of every socket write:
        # it only counts frames/pongs that actually arrived. Started after the
        # servers so a connection can never be adopted without a monitor.
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(), name="dot2-watchdog"
            )

    async def stop(self) -> None:
        self._closing = True
        watchdog = self._watchdog_task
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        for conn in (self._control, self._data):
            if conn is not None:
                # A device that just lost power never answers a close frame, and
                # websockets would wait its 10 s close timeout on each socket.
                try:
                    await asyncio.wait_for(_quiet_close(conn), CLOSE_TIMEOUT_S)
                except TimeoutError:
                    self._abandon(conn, 1000, "stop")
        self._control = None
        self._data = None
        for server in self._servers:
            server.close()
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
        # Nothing that belonged to a connection may survive stop(): open() on the
        # next incarnation must start from a truly empty link.
        self._clear_link_state()
        self._meters.clear()
        # close() must stop future events too: the queue is drained by rui.
        self.wakes.clear()
        # Buffered playback belongs to the previous connection even when the
        # adapter is the one closing; reset it so open() starts clean.
        self._notify_link_lost("link stopped")

    def _clear_link_state(self) -> None:
        """Forget every piece of state that belonged to a connection.

        Config and targets (volume cap/level, ``oww_*``, aec, token, features)
        are ours and survive: the device re-registers from scratch and must not
        inherit a slot, a gate, a waiter or a counter from its last incarnation.
        """
        self._control = None
        self._data = None
        self.connected.clear()
        self._inflight_bytes = 0
        self._pong_waiters.clear()
        self._pong_events.clear()
        self._listen_state = ""
        self._scoring_evidence = False
        self._reported_level = None
        # No probe loop may outlive the plane it probed (unless it is the caller,
        # e.g. a ping that lost its own socket).
        task = self._ping_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._ping_task = None
        # Fresh locks: a sender parked on the old socket owns the old object, and
        # waiting behind it is exactly what kept a reconnect from being acked.
        # Sends re-check the slot identity under the lock, so a stale sender can
        # never write into the connection that replaced it.
        self._control_lock = asyncio.Lock()
        self._data_lock = asyncio.Lock()

    def _meter(self, conn: ServerConnection, direction: str) -> _ConnMeter:
        meter = _ConnMeter(direction=direction, peer=str(conn.remote_address))
        self._meters[id(conn)] = meter
        return meter

    # ---------------------------------------------------------- liveness (P0b-C1)
    @property
    def control_plane_up(self) -> bool:
        """True while a registered /control socket is adopted."""
        return self._control is not None

    @property
    def data_plane_up(self) -> bool:
        """True while a /data socket is adopted (the speaker can be written to)."""
        return self._data is not None

    async def _watchdog_loop(self) -> None:
        """Poll the receive side of the link; never touch the write path.

        A dedicated task (instead of per-connection timers) means one monitor
        for both planes and it cannot be blocked by a send() parked on a full
        buffer - the exact failure the websockets keepalive has.
        """
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            try:
                self._enforce_half_open()
            except Exception:  # noqa: BLE001 - a monitor bug must not die silently
                log.exception("dot2 link: half-open watchdog failed")

    def _enforce_half_open(self) -> None:
        """Drop the link when no plane has received anything for the timeout.

        Liveness is evaluated across the whole link, not per socket: an idle
        speaker still streams mic frames every 80 ms even while a pong is in
        flight, and a device that is demonstrably alive must not lose its
        registration because /control happened to be quiet for a window. A
        single stalled *write* is caught by the bounded writes above (control
        3 s, /data 0.75 s + watermark); this watchdog exists for the case where
        no write is even attempted and the sockets look open forever.

        The 887 s zombie had 887 s with no received packet on either plane, so
        the "every plane silent" rule covers it without adding a false-positive
        path for a live, quiet room.
        """
        if self._closing:
            return
        now = time.monotonic()
        planes: list[tuple[ServerConnection, _ConnMeter, float]] = []
        for conn in (self._control, self._data):
            if conn is None:
                continue
            meter = self._meters.get(id(conn))
            if meter is None:
                continue
            planes.append((conn, meter, now - meter.last_rx_at))
        if not planes:
            return
        if not all(age > HALF_OPEN_TIMEOUT_S for _conn, _meter, age in planes):
            return
        detail = "; ".join(
            f"dir={meter.direction} silent={age:.1f}s rx={meter.rx_bytes}B tx={meter.tx_bytes}B"
            for _conn, meter, age in planes
        )
        reason = (
            f"half-open: no frame or pong on any plane for > {HALF_OPEN_TIMEOUT_S:.1f} s "
            f"({detail})"
        )
        self.stats.half_open_drops += 1
        log.warning(
            "dot2 link: %s (%s); aborting the link so the device dials again",
            reason,
            self.device_id,
        )
        control = self._control
        if control is not None:
            # Tears down /data as well; both belong to the same dead link.
            self._lose_control(control, reason)
        data = self._data
        if data is not None:
            self._lose_data(data, reason, abort=True)

    def _abandon(self, conn: ServerConnection, code: int, reason: str) -> None:
        """Drop a socket without waiting for its close handshake.

        ``conn.close()`` waits for the peer's close frame (10 s by default) and a
        peer that just lost power never answers it. Callers here are holding a
        link slot, so they must not await a dead socket.
        """
        transport = getattr(conn, "transport", None)
        if transport is not None:
            try:
                transport.abort()
                return
            except Exception:  # noqa: BLE001 - abort is best-effort
                log.debug("dot2 link: transport.abort failed", exc_info=True)
        asyncio.create_task(_quiet_close(conn, code, reason))

    async def _refuse(self, conn: ServerConnection, code: int, reason: str) -> None:
        """Tell a live-but-wrong peer why, without hanging on its close echo."""
        try:
            await asyncio.wait_for(_quiet_close(conn, code, reason), CLOSE_TIMEOUT_S)
        except TimeoutError:
            self._abandon(conn, code, reason)

    def _force_clean_cycle(self, reason: str) -> None:
        """Give up on a device that never registered.

        Only runs while no control plane owns the link, so it can never touch a
        live registration: any /data socket is an orphan from the same failed
        attempt (or from before a power cut) and is dropped so the device dials
        both planes itself instead of writing into a slot it cannot use.
        """
        if self._closing or self._control is not None:
            return
        orphan = self._data
        self._clear_link_state()
        if orphan is not None:
            self._abandon(orphan, 1011, reason)
            log.info("dot2 link: dropped the orphaned data plane: %s", reason)
        self._notify_link_lost(reason)

    def _adopt_control(self, conn: ServerConnection) -> None:
        """Make ``conn`` the one control plane and retire the previous one.

        A device that reconnects before the bridge notices its old socket died
        leaves two live control planes. Keeping the old one open leaves its
        handler in a read loop that would later clean up state now belonging to
        the new registration, so it is retired here - synchronously, because a
        dead peer's close handshake is what we cannot wait for.
        """
        previous = self._control
        if previous is not None and previous is not conn:
            old = self._meters.get(id(previous))
            old_task = old.ping_task if old is not None else None
            if old_task is not None:
                old_task.cancel()
                if old is not None:
                    old.ping_task = None
                if self._ping_task is old_task:
                    self._ping_task = None
            self._abandon(previous, 1011, "superseded by a new register")
            log.info("dot2 link: retired the superseded control plane (%s)", self.device_id)
        self._control = conn
        # A reconnect can land on a device whose config changed under us: the
        # listen state, the proof that it can score and the last volume it
        # reported all belong to the connection that is being replaced.
        self._listen_state = ""
        self._scoring_evidence = False
        self._reported_level = None
        # Pongs for pings the old socket never answered must not be counted as
        # this connection's.
        self._pong_waiters.clear()
        self._pong_events.clear()

    def _adopt_data(self, conn: ServerConnection) -> None:
        """Make ``conn`` the data plane, retiring a superseded one first."""
        previous = self._data
        if previous is not None and previous is not conn:
            self._abandon(previous, 1011, "superseded by a new data plane")
            log.info("dot2 link: retired the superseded data plane (%s)", self.device_id)
        self._data = conn
        self._inflight_bytes = 0

    def _teardown_control(self, reason: str) -> None:
        """Take the whole link down: no slot, no gate, no orphan /data.

        ``_clear_link_state`` first, so any sender that was parked on the old
        socket sees a slot that no longer matches it and writes nothing.
        """
        orphan = self._data
        self._clear_link_state()
        if orphan is not None and not self._closing:
            # Without /control nothing can be ordered on the device (flush,
            # barrier, volume) and the next register must land on sockets the
            # device dialled itself: a half-open /data is exactly the stale slot
            # that a bridge restart used to clear.
            self._abandon(orphan, 1011, reason)
        if not self._closing:
            self._notify_link_lost(reason)

    def _lose_control(self, conn: ServerConnection, reason: str) -> None:
        """Drop a control plane whose frames no longer reach the device."""
        if conn is not self._control:
            return
        meter = self._meters.get(id(conn))
        if meter is not None:
            # The close line below says everything the handler's would.
            meter.accounted = True
            task = meter.ping_task
            # ...and a probe loop with no plane to probe stops now, not on its
            # next tick (unless this call is the loop itself).
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                if self._ping_task is task:
                    self._ping_task = None
        log.warning("dot2 link: %s (%s) dir=control; dropping the link", reason, self.device_id)
        self._abandon(conn, 1011, reason)
        self._teardown_control(reason)

    def _drop_control(
        self,
        conn: ServerConnection,
        meter: _ConnMeter,
        closed: BaseException | None,
    ) -> None:
        """Clean up after one control connection, whoever owns the slot now."""
        self._meters.pop(id(conn), None)
        code = _close_code(closed, conn)
        task = meter.ping_task
        if task is not None:
            # Only this connection's own probe loop: cancelling whatever is in
            # ``self._ping_task`` killed the live registration's liveness.
            task.cancel()
            if self._ping_task is task:
                self._ping_task = None
        if self._control is not conn:
            if not meter.accounted:
                log.info(
                    "dot2 link: late control close (%s) code=%s dir=%s rx=%dB tx=%dB",
                    self.device_id,
                    code,
                    meter.direction,
                    meter.rx_bytes,
                    meter.tx_bytes,
                )
            return
        log_line = "dot2 link: control closed (%s) code=%s dir=%s rx=%dB tx=%dB"
        args = (self.device_id, code, meter.direction, meter.rx_bytes, meter.tx_bytes)
        if self._closing or code in {"1000", "1001"}:
            # Our own shutdown, or a device that said goodbye on purpose: not a
            # fault, so it must not read as one in the log.
            log.info(log_line, *args)
        else:
            log.warning(log_line, *args)
        self._teardown_control("control plane closed")

    def _notify_link_lost(self, reason: str) -> None:
        callback = self.on_link_lost
        if callback is None:
            return
        try:
            callback(reason)
        except Exception:  # noqa: BLE001 - a handler bug must not kill the link
            log.exception("dot2 link: on_link_lost handler failed (%s)", reason)

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
            log.warning(
                "dot2 link: refused %s from %s: reason=token mismatch", path, conn.remote_address
            )
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
        """Run one /control connection: register, then read until it dies.

        Both phases live inside one try/finally so there is no window - a close
        during the ack, the config push or ``mic_start`` included - in which a
        slot, a gate or a ping task can outlive its socket.
        """
        meter = self._meter(conn, "control")
        closed: BaseException | None = None
        try:
            if not await self._register_control(conn, meter):
                return
            async for raw in conn:
                meter.rx_bytes += len(raw)
                meter.last_rx_at = time.monotonic()
                await self._on_control_message(raw)
        except ConnectionClosed as exc:
            closed = exc
        except Exception:  # noqa: BLE001 - a handler bug must not leak the slot
            log.exception("dot2 link: control handler failed (%s)", self.device_id)
        finally:
            self._drop_control(conn, meter, closed)

    async def _register_control(self, conn: ServerConnection, meter: _ConnMeter) -> bool:
        """Read the first frame and adopt ``conn`` as the control plane.

        Every failure logs *why*: a silent 10 s close is what made the
        2026-10-28 loop look like a firmware fault while a bridge restart (fresh
        state) recovered in 3 s.
        """
        peer = meter.peer
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "dot2 link: /control handshake timed out after %.1f s: register did not "
                "arrive (peer=%s rx=%dB); dropping the link so the device can dial again",
                HANDSHAKE_TIMEOUT_S,
                peer,
                meter.rx_bytes,
            )
            self._force_clean_cycle("register did not arrive")
            self._abandon(conn, 1011, "register did not arrive")
            meter.accounted = True
            return False
        except ConnectionClosed as exc:
            log.info(
                "dot2 link: /control closed before register (peer=%s code=%s rx=%dB)",
                peer,
                _close_code(exc, conn),
                meter.rx_bytes,
            )
            self._force_clean_cycle("control closed before register")
            meter.accounted = True
            return False
        meter.rx_bytes += len(raw)
        meter.last_rx_at = time.monotonic()
        try:
            # errors="replace": a malformed frame reads as "not register" instead
            # of raising out of a handler that has not claimed the slot yet.
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            message = json.loads(text)
        except ValueError:
            log.warning(
                "dot2 link: register rejected: reason=first frame is not JSON (peer=%s rx=%dB)",
                peer,
                meter.rx_bytes,
            )
            self._force_clean_cycle("bad register")
            await self._refuse(conn, 1002, "bad register")
            meter.accounted = True
            return False
        if not isinstance(message, dict) or message.get("type") != "register":
            kind = message.get("type") if isinstance(message, dict) else type(message).__name__
            log.warning(
                "dot2 link: register rejected: reason=first message was %r, expected register "
                "(peer=%s rx=%dB)",
                kind,
                peer,
                meter.rx_bytes,
            )
            self._force_clean_cycle("expected register")
            await self._refuse(conn, 1002, "expected register")
            meter.accounted = True
            return False

        self.info = DeviceInfoReport(
            device_id=str(message.get("device_id") or ""),
            version=str(message.get("version") or ""),
            capabilities=tuple(message.get("capabilities") or ()),
            ip=str(message.get("ip") or ""),
            base_os=str(message.get("base_os") or ""),
            raw=message,
        )
        self.device_id = self.device_id or self.info.device_id
        self._adopt_control(conn)
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
                    **({"owwOnDevice": self.oww_on_device}
                       if self.oww_on_device is not None else {}),
                    # The device opens the scorer for this model; omitting it leaves
                    # the firmware default (hey_jarvis_v0.1), which is only correct
                    # by accident. The threshold is the bar a score must clear.
                    **({"owwModel": self.oww_model} if self.oww_model else {}),
                    **({"owwThreshold": float(self.oww_threshold)}
                       if self.oww_threshold is not None else {}),
                    "wakeWordEnabled": self.wake_word_enabled,
                    "aecEnabled": self.aec_enabled,
                    **({"micGainDb": self.mic_gain_db} if self.mic_gain_db is not None else {}),
                    **({"aecTailMs": self.aec_tail_ms} if self.aec_tail_ms is not None else {}),
                    **({"aecDelayMs": self.aec_delay_ms} if self.aec_delay_ms is not None else {}),
                    **({"agcEnabled": self.agc_enabled} if self.agc_enabled is not None else {}),
                    **({"bargeInEnabled": self.barge_in_enabled}
                       if self.barge_in_enabled is not None else {}),
                    **({"bargeInThreshold": self.barge_in_threshold}
                       if self.barge_in_threshold is not None else {}),
                    **({"wakeMic": self.wake_mic} if self.wake_mic is not None else {}),
                    **({"aecRefSource": self.aec_ref_source}
                       if self.aec_ref_source is not None else {}),
                }
            )
        await self._send_control({"type": "mic_start", "lock_mic": False})

        # Owned by this connection only: see _drop_control, which cancels this
        # exact task and never whatever a newer registration installed.
        meter.ping_task = asyncio.create_task(self._ping_loop(conn), name="dot2-ping")
        self._ping_task = meter.ping_task
        return True

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
            self._listen_state = str(message.get("state") or "").strip().lower()
            log.info("dot2 link: listen_state=%s", self._listen_state or "?")
        elif kind == "oww_wake":
            self._on_oww_wake(message)
        elif kind == "oww_shadow_cross":
            self._on_oww_shadow_cross(message)
        else:
            log.debug("dot2 link: control %s", kind)

    def _on_oww_shadow_cross(self, message: dict[str, Any]) -> None:
        """Record a shadow crossing: proof the device scored, but never a wake.

        Shadow mode is the firmware scoring our stream without acting on it. It is
        not a DeviceWake (nothing crossed the configured bar for a turn) and must
        never enter the queue, but it is the same evidence that the runtime and the
        model are loaded and running on the device.
        """
        self.stats.shadow_crossings += 1
        self._scoring_evidence = True
        log.debug(
            "dot2 link: oww_shadow_cross score=%.3f ageMs=%s (scoring proved)",
            float(message.get("score") or 0.0),
            message.get("ageMs"),
        )

    def _on_oww_wake(self, message: dict[str, Any]) -> None:
        """Queue one device-detected wake; an invalid report is counted, not raised."""
        event, reason = map_oww_wake(message, now_ms=time.monotonic() * 1000.0)
        if event is None:
            self.stats.invalid_wakes += 1
            log.warning("dot2 link: ignored oww_wake (%s)", reason)
            return
        self.stats.wake_events += 1
        # A valid oww_wake is the device stating it scored with the model and
        # crossed the threshold: proof of on-device wake even when the firmware
        # keeps streaming (listen_state stays "stream" without listen_session).
        self._scoring_evidence = True
        if not event.barge and self.playing is not None and self.playing():
            # Stream-mode firmware cannot flag barge (it only does inside a private
            # listen session); the adapter knows whether its own playback was live,
            # and a wake during a reply is exactly the interruption rui must handle.
            event = replace(event, barge=True)
            log.info(
                "dot2 link: oww_wake during playback treated as barge-in "
                "(score %.2f, level %s)",
                event.score,
                event.level,
            )
        self.wakes.push(event)
        if event.barge:
            log.info(
                "dot2 link: oww_wake barge hint (score %.2f, level %s)",
                event.score,
                event.level,
            )
        else:
            log.debug("dot2 link: oww_wake queued (score %.2f)", event.score)

    def drain_wakes(self) -> list:
        """Return every pending device wake once (v1 section 4.2); [] after close."""
        if self._closing:
            return []
        return self.wakes.drain()

    @property
    def scoring_evidence(self) -> bool:
        """Whether the device has proved it really scores (wake or shadow crossing)."""
        return self._scoring_evidence

    @property
    def device_wake_available(self) -> bool:
        """Whether the live link proves on-device scoring of OUR wake word.

        Two honest proofs, both requiring the controller to have opted in
        (``owwOnDevice=on``) and the firmware to advertise a local-scoring feature:

        * ``listen_state="local"`` - the scorer owns the microphone (private
          listening; needs a controller that implements listen sessions);
        * a valid ``oww_wake`` or an ``oww_shadow_cross`` - the device actually ran
          the ONNX scorer on our stream and produced a decision. This is the path
          for streaming firmware: the model runs on the device, the mic keeps
          streaming to rui, and the crossing is the wake.

        Advertising device_wake optimistically would be a contract violation, so
        anything less than one of these is a false.
        """
        if self.oww_on_device != "on":
            return False
        capabilities = {str(capability).lower() for capability in self.info.capabilities}
        if not (capabilities & {"oww_local_only", "oww_trigger", "oww_shadow"}):
            return False
        return self._listen_state == "local" or self._scoring_evidence

    async def _ping_loop(self, conn: ServerConnection | None = None) -> None:
        """Probe the device over one specific control connection.

        Bound to ``conn`` on purpose: a superseded connection's loop must stop
        instead of probing whatever registration came after it, and it must stay
        identifiable so its own handler cancels only itself.
        """
        conn = conn if conn is not None else self._control
        if conn is None:
            return
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if self._control is not conn:
                return
            now = time.monotonic()
            # A pong for a connection the device already abandoned never
            # arrives; forgetting the waiter keeps this dict bounded.
            for stale_id, sent_at in list(self._pong_waiters.items()):
                if now - sent_at > PONG_WAITER_TTL_S:
                    self._pong_waiters.pop(stale_id, None)
            self._ping_seq += 1
            ping_id = self._ping_seq
            self._pong_waiters[ping_id] = time.monotonic()
            try:
                await self._send_control({"type": "ping", "id": ping_id})
            except (ConnectionClosed, RuntimeError):
                return

    def _on_pong(self, message: dict[str, Any]) -> None:
        ping_id = message.get("id")
        if not isinstance(ping_id, int):
            return
        sent_at = self._pong_waiters.pop(ping_id, None)
        if sent_at is not None:
            self.stats.rtt_ms = round((time.monotonic() - sent_at) * 1000.0, 1)
        event = self._pong_events.get(ping_id)
        if event is not None:
            event.set()

    # ------------------------------------------------------------------ data
    async def _serve_data(self, conn: ServerConnection) -> None:
        meter = self._meter(conn, "data")
        self._adopt_data(conn)
        if self._control is not None and not self._closing:
            # The link is only usable for playback when both halves are here.
            # A data reconnect restores the gate without waiting for a register.
            self.connected.set()
        self.stats.data_connects += 1
        log.info("dot2 link: data plane connected (%s)", self.device_id)
        closed: BaseException | None = None
        try:
            async for raw in conn:
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    continue
                meter.rx_bytes += len(raw)
                meter.last_rx_at = time.monotonic()
                kind = raw[0]
                payload = bytes(raw[1:])
                if kind == FRAME_MIC:
                    # [0x01][seq u16 BE][PCM]; the sequence is not audio.
                    if len(raw) < 3:
                        continue
                    payload = bytes(raw[3:])
                    control = mic_control(payload) if len(payload) == 1 else None
                    if control is not None:
                        log.debug("dot2 link: mic control 0x%02x", control)
                        continue
                    self._enqueue_audio(payload)
                elif kind in (FRAME_VAD_END, FRAME_NO_SPEECH_TIMEOUT):
                    log.debug("dot2 link: turn sentinel 0x%02x", kind)
                else:
                    log.debug("dot2 link: data frame 0x%02x (%d B)", kind, len(payload))
        except ConnectionClosed as exc:
            closed = exc
        finally:
            self._lose_data(
                conn,
                "data plane closed",
                abort=False,
                quiet=self._closing,
                meter=meter,
                closed=closed,
            )

    def _lose_data(
        self,
        conn: ServerConnection,
        reason: str,
        *,
        abort: bool,
        quiet: bool = False,
        meter: _ConnMeter | None = None,
        closed: BaseException | None = None,
    ) -> None:
        """Drop ``conn`` as the data plane and let the device dial again.

        ``abort=True`` kills the TCP socket (a graceful close would itself wait
        on the same full write buffer). The identity check keeps an old socket's
        late timeout from tearing down a connection the device already replaced.
        """
        meter = meter if meter is not None else self._meters.get(id(conn))
        self._meters.pop(id(conn), None)
        code = _close_code(closed, conn)
        rx = meter.rx_bytes if meter is not None else 0
        tx = meter.tx_bytes if meter is not None else 0
        if conn is not self._data:
            log.info(
                "dot2 link: late data close (%s) code=%s dir=%s rx=%dB tx=%dB",
                self.device_id,
                code,
                meter.direction if meter is not None else "data",
                rx,
                tx,
            )
            return
        self._data = None
        self._inflight_bytes = 0
        self.connected.clear()
        # A stalled write is a failure even when the device is still registered
        # on /control: playback is broken, so the gate must not claim otherwise.
        (log.info if quiet else log.warning)(
            "dot2 link: %s (%s) code=%s dir=%s rx=%dB tx=%dB; %s",
            reason,
            self.device_id,
            code,
            meter.direction if meter is not None else "data",
            rx,
            tx,
            "aborting /data so the device reconnects" if abort else "data plane down",
        )
        if abort:
            self._abandon(conn, 1011, "write stalled")
        self._notify_link_lost(reason)

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
        """What this controller can do, announced in ``ack.features``."""
        features: list[str] = []
        if self.ref_uplink:
            features.append("dot2_hw_ref")
        return tuple(features)

    async def _send_control(self, message: dict[str, Any]) -> None:
        conn = self._control
        if conn is None:
            raise RuntimeError("device control plane is not connected")
        text = json.dumps(message, separators=(",", ":"))
        try:
            async with asyncio.timeout(CONTROL_WRITE_TIMEOUT_S):
                async with self._control_lock:
                    if conn is not self._control:
                        # The slot moved while this message waited: it belongs to
                        # a socket the device already replaced.
                        log.debug(
                            "dot2 link: dropped %s for a superseded control plane",
                            message.get("type"),
                        )
                        return
                    await conn.send(text)
        except TimeoutError:
            self._lose_control(
                conn, f"/control write stalled > {CONTROL_WRITE_TIMEOUT_S:.1f} s"
            )
            raise RuntimeError("device control plane is blocked") from None
        meter = self._meters.get(id(conn))
        if meter is not None:
            meter.tx_bytes += len(text)

    async def _write_data_frame(self, conn: ServerConnection, frame: bytes) -> bool:
        """Write one /data frame under the data lock with a hard bound.

        Returning False means the frame was not written and the data plane has
        been dropped; callers must not raise into the audio plane on link loss.
        """
        async with self._data_lock:
            if conn is not self._data:
                return False
            try:
                async with asyncio.timeout(WRITE_TIMEOUT_S):
                    await conn.send(frame)
            except TimeoutError:
                self._lose_data(
                    conn,
                    f"/data write stalled > {WRITE_TIMEOUT_S:.2f} s",
                    abort=True,
                )
                return False
            except ConnectionClosed as exc:
                self._lose_data(
                    conn, "data plane closed during write", abort=False, closed=exc
                )
                return False
            meter = self._meters.get(id(conn))
            if meter is not None:
                meter.tx_bytes += len(frame)
            self.stats.last_pcm_sent_at = time.monotonic()
            return True

    async def send_pcm(self, pcm: bytes) -> bool:
        """Queue 48 kHz mono PCM as ``0x02`` frames (whole frames only).

        Returns False when the frame was dropped because the data plane is down
        or was just aborted. A stalled write aborts /data instead of waiting on
        a full socket buffer forever; the adapter (the audio plane) sees a bool,
        never a timeout exception.
        """
        conn = self._data
        if conn is None:
            log.debug("dot2 link: dropped %d B of PCM: data plane not connected", len(pcm))
            return False
        frames = split_playback(pcm)
        if not frames:
            return True
        total = sum(len(frame) for frame in frames)
        self._inflight_bytes += total
        try:
            if self._inflight_bytes > SEND_WATERMARK_BYTES:
                self._lose_data(
                    conn,
                    f"outbound PCM watermark exceeded "
                    f"({self._inflight_bytes} B > {SEND_WATERMARK_BYTES} B)",
                    abort=True,
                )
                return False
            for frame in frames:
                if not await self._write_data_frame(conn, frame):
                    return False
                self.stats.speaker_frames += 1
            return True
        finally:
            self._inflight_bytes = max(0, self._inflight_bytes - total)

    async def end_stream(self) -> None:
        conn = self._data
        if conn is None:
            return
        await self._write_data_frame(conn, speaker_eos())

    async def flush(self) -> None:
        """Discard buffered playback, then wait until the device has seen it.

        speaker_flush travels on /control while PCM travels on /data - two TCP
        connections with no cross-connection ordering, so the device could accept
        the next reply's first blocks before the flush and then throw them away
        (the 406 ms hole, 2026-10-24). The ping rides the same /control socket,
        the device answers pings immediately and in order, so a pong proves the
        flush was applied. Keep sending the flush: it also opens the device's
        prime bypass; skipping it makes every reply wait the 1.024 s prime gate.
        """
        if self._control is None:
            # Nothing to flush on a link the device already left; a broken link
            # must not turn the next reply's stop_playback() into an exception.
            return
        try:
            await self._send_control({"type": "speaker_flush"})
        except (ConnectionClosed, RuntimeError):
            return
        await self._barrier()

    async def _barrier(self, timeout: float = 0.8) -> bool:
        """Send a ping and wait for its pong: orders /control against later sends."""
        if self._control is None:
            # A lost link has no device buffer left to order against; the next
            # reply must be a no-op, not an exception in rui's audio plane.
            return False
        self._ping_seq += 1
        ping_id = self._ping_seq
        event = asyncio.Event()
        self._pong_events[ping_id] = event
        self._pong_waiters[ping_id] = time.monotonic()
        try:
            await self._send_control({"type": "ping", "id": ping_id})
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except (TimeoutError, ConnectionClosed, RuntimeError):
            log.warning("dot2 link: flush barrier timed out")
            return False
        finally:
            self._pong_events.pop(ping_id, None)
            self._pong_waiters.pop(ping_id, None)

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
        try:
            await self._send_control({"type": "leds", "leds": leds, "listening": listening})
        except (ConnectionClosed, RuntimeError):
            # A repaint is cosmetic; it must never be the thing that raises into
            # the caller's loop when the device is gone.
            return


async def _send_json(conn: ServerConnection, payload: dict[str, Any]) -> None:
    try:
        await conn.send(json.dumps(payload, separators=(",", ":")))
    except ConnectionClosed:
        pass


def _close_code(closed: BaseException | None, conn: ServerConnection) -> str:
    """The wire close code for a close line: received, else sent, else current."""
    if isinstance(closed, ConnectionClosed):
        for frame in (getattr(closed, "rcvd", None), getattr(closed, "sent", None)):
            code = getattr(frame, "code", None)
            if code is not None:
                return str(code)
    code = getattr(conn, "close_code", None)
    return str(code) if code is not None else "-"


async def _quiet_close(conn: ServerConnection, code: int = 1000, reason: str = "") -> None:
    try:
        await conn.close(code, reason)
    except Exception:  # noqa: BLE001 - closing must never raise into the caller
        pass
