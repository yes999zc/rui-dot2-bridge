"""DeviceLink integration tests over a plain websocket (no TLS, no hardware).

These pin the parts of ``docs/protocol-map.md`` a wrong guess would break:
the register/ack handshake, the volume ceiling, mic enqueueing, speaker
framing and the flush that makes barge-in audible as silence.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket
import time
import unittest
from unittest import mock

import websockets.asyncio.client as ws_client
from rui.device.contract import Capability, wake_validation_error
from websockets.exceptions import ConnectionClosed

import rui_dot2_bridge.device_link as device_link_module
from rui_dot2_bridge.adapter import Dot2Adapter
from rui_dot2_bridge.device_link import DeviceLink
from rui_dot2_bridge.frames import (
    FRAME_MIC,
    FRAME_SPEAKER,
    FRAME_SPEAKER_EOS,
    SPEAKER_CHUNK_BYTES,
)
from rui_dot2_bridge.volume import capped_level


class LinkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.link = DeviceLink(
            tls_port=None,
            plain_port=0,
            token="secret",
            volume_cap_percent=30.0,
            push_config=True,
        )
        await self.link.start()
        self.port = self.link.bound_ports["plain"]
        self.addCleanup(self.link.stop)

    @contextlib.asynccontextmanager
    async def _control(
        self,
        token: str = "secret",
        device_id: str = "D090",
        capabilities: list[str] | None = None,
    ):
        """Open /control and complete the handshake, yielding the socket."""
        headers = {"X-EM-Token": token} if token else {}
        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/control", additional_headers=headers
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": device_id,
                        "version": "v2.18.1",
                        "capabilities": capabilities or ["mic", "speaker"],
                        "ip": "<lan-ip>",
                    }
                )
            )
            yield ws

    async def _handshake(self, ctl) -> None:
        """Consume ack, volume_set, config and mic_start."""
        for _ in range(4):
            await asyncio.wait_for(ctl.recv(), 2)

    async def test_handshake_messages(self) -> None:
        async with self._control() as ws:
            messages = [json.loads(await asyncio.wait_for(ws.recv(), 2)) for _ in range(4)]
        kinds = [m["type"] for m in messages]
        self.assertEqual(kinds, ["ack", "volume_set", "config", "mic_start"])
        self.assertEqual(messages[1]["level"], capped_level(30.0))
        self.assertFalse(messages[3]["lock_mic"])

    async def test_token_refused(self) -> None:
        from websockets.exceptions import ConnectionClosed

        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/control",
            additional_headers={"X-EM-Token": "wrong"},
        ) as ws:
            refusal = None
            try:
                await ws.send(json.dumps({"type": "register", "device_id": "X"}))
                message = json.loads(await asyncio.wait_for(ws.recv(), 2))
                self.assertEqual(message["type"], "refused")
            except (ConnectionClosed, TimeoutError) as closed:
                # Closing right after (or instead of) "refused" is also correct: the
                # client can lose the race with the close frame and 1008 says why.
                refusal = closed
            if refusal is not None:
                self.assertIsInstance(refusal, (ConnectionClosed, TimeoutError))

    async def test_mic_frames_are_queued(self) -> None:
        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/data", additional_headers={"X-EM-Token": "secret"}
        ) as ws:
            await asyncio.sleep(0.1)
            payload = bytes(2560)
            await ws.send(bytes([FRAME_MIC]) + b"\x00\x01" + payload)
            chunk = await asyncio.wait_for(self.link.audio.get(), 2)
            self.assertEqual(chunk, payload)
        self.assertEqual(self.link.stats.mic_frames, 1)
        self.assertEqual(self.link.stats.mic_bytes, 2560)

    async def test_speaker_frames_and_eos(self) -> None:
        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/data", additional_headers={"X-EM-Token": "secret"}
        ) as data_ws:
            await asyncio.sleep(0.1)
            pcm = bytes(SPEAKER_CHUNK_BYTES * 2)
            await self.link.send_pcm(pcm)
            first = await asyncio.wait_for(data_ws.recv(), 2)
            second = await asyncio.wait_for(data_ws.recv(), 2)
            self.assertEqual(first[0], FRAME_SPEAKER)
            self.assertEqual(second[0], FRAME_SPEAKER)
            self.assertEqual(len(first) + len(second), len(pcm) + 2)
            await self.link.end_stream()
            eos = await asyncio.wait_for(data_ws.recv(), 2)
            self.assertEqual(eos, bytes([FRAME_SPEAKER_EOS]))

    async def test_flush_is_control_message(self) -> None:
        async with self._control() as ctl:
            for _ in range(4):  # ack, volume_set, config, mic_start
                await asyncio.wait_for(ctl.recv(), 2)
            await self.link.flush()
            message = json.loads(await asyncio.wait_for(ctl.recv(), 2))
            self.assertEqual(message["type"], "speaker_flush")

    async def test_volume_ceiling_is_enforced_on_device_events(self) -> None:
        async with self._control() as ctl:
            for _ in range(4):
                await asyncio.wait_for(ctl.recv(), 2)
            # The user pressed volume up: the device reports a level above the cap.
            await ctl.send(json.dumps({"type": "volume_state", "level": 110}))
            message = json.loads(await asyncio.wait_for(ctl.recv(), 2))
            self.assertEqual(message["type"], "volume_set")
            self.assertEqual(message["level"], capped_level(30.0))

    async def test_stale_low_report_does_not_lower_the_target(self) -> None:
        async with self._control() as ctl:
            for _ in range(4):
                await asyncio.wait_for(ctl.recv(), 2)
            # The device reports its pre-push 38 after we already sent our target.
            await ctl.send(json.dumps({"type": "volume_state", "level": 38}))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ctl.recv(), 0.4)
            # A later report above the ceiling still gets corrected.
            await ctl.send(json.dumps({"type": "volume_state", "level": 127}))
            message = json.loads(await asyncio.wait_for(ctl.recv(), 2))
            self.assertEqual(message["type"], "volume_set")
            self.assertEqual(message["level"], capped_level(30.0))

    async def test_volume_below_cap_is_accepted(self) -> None:
        async with self._control() as ctl:
            for _ in range(4):
                await asyncio.wait_for(ctl.recv(), 2)
            await ctl.send(json.dumps({"type": "volume_state", "level": 10}))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ctl.recv(), 0.4)

    async def test_oww_wake_is_queued_and_drained_once(self) -> None:
        async with self._control() as ctl:
            await self._handshake(ctl)
            await ctl.send(
                json.dumps(
                    {
                        "type": "oww_wake",
                        "score": 0.92,
                        "threshold": 0.5,
                        "ageMs": 120,
                        "capturedMono": True,
                        "barge": False,
                        "level": -18.5,
                    }
                )
            )
            await asyncio.sleep(0.05)
            events = self.link.drain_wakes()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertAlmostEqual(event.score, 0.92)
            self.assertAlmostEqual(event.threshold, 0.5)
            self.assertIs(event.barge, False)
            self.assertEqual(event.source, "device")
            self.assertEqual(self.link.drain_wakes(), [])
        self.assertEqual(self.link.stats.wake_events, 1)
        self.assertEqual(self.link.stats.invalid_wakes, 0)

    async def test_invalid_oww_wake_is_counted_not_queued(self) -> None:
        async with self._control() as ctl:
            await self._handshake(ctl)
            for message in (
                {"type": "oww_wake", "score": 0.2, "threshold": 0.5},
                {"type": "oww_wake", "score": 0.9, "threshold": 0.5, "barge": "maybe"},
                {"type": "oww_wake", "score": 0.9, "threshold": 0},
            ):
                await ctl.send(json.dumps(message))
            await asyncio.sleep(0.1)
            self.assertEqual(self.link.drain_wakes(), [])
        self.assertEqual(self.link.stats.invalid_wakes, 3)
        self.assertEqual(self.link.stats.wake_events, 0)

    async def test_wake_queue_is_bounded_to_32(self) -> None:
        async with self._control() as ctl:
            await self._handshake(ctl)
            for index in range(40):
                await ctl.send(
                    json.dumps(
                        {"type": "oww_wake", "score": 0.9, "threshold": 0.5, "ageMs": index}
                    )
                )
            await asyncio.sleep(0.3)
            events = self.link.drain_wakes()
        self.assertEqual(len(events), 32)
        self.assertEqual(self.link.wakes.dropped, 8)
        self.assertGreater(events[0].ts_ms, events[-1].ts_ms)

    async def test_device_wake_needs_local_scorer_and_listen_state(self) -> None:
        self.link.oww_on_device = "on"
        async with self._control() as ctl:
            await self._handshake(ctl)
            self.assertFalse(self.link.device_wake_available)
        async with self._control(capabilities=["mic", "speaker", "oww_local_only"]) as ctl:
            await self._handshake(ctl)
            await ctl.send(json.dumps({"type": "listen_state", "state": "remote"}))
            await asyncio.sleep(0.05)
            self.assertFalse(self.link.device_wake_available)
            await ctl.send(json.dumps({"type": "listen_state", "state": "local"}))
            await asyncio.sleep(0.05)
            self.assertTrue(self.link.device_wake_available)

    async def test_streaming_scoring_evidence_upgrades_device_wake(self) -> None:
        """Streaming firmware keeps listen_state=stream; a real oww_wake is proof.

        The device only emits oww_wake when it ran the ONNX scorer and crossed the
        threshold, so after one valid event the plug may advertise device_wake even
        though the microphone never left the controller's stream.
        """
        self.link.oww_on_device = "on"
        async with self._control(
            capabilities=["mic", "speaker", "oww_local_only", "oww_trigger"]
        ) as ctl:
            await self._handshake(ctl)
            await ctl.send(json.dumps({"type": "listen_state", "state": "stream"}))
            await asyncio.sleep(0.05)
            self.assertFalse(self.link.device_wake_available)
            await ctl.send(
                json.dumps({"type": "oww_wake", "score": 0.94, "threshold": 0.5})
            )
            await asyncio.sleep(0.05)
            self.assertTrue(self.link.scoring_evidence)
            self.assertTrue(self.link.device_wake_available)
            self.assertEqual(len(self.link.drain_wakes()), 1)

    async def test_shadow_crossing_proves_scoring_without_a_wake(self) -> None:
        """A shadow crossing is evidence, never a DeviceWake."""
        self.link.oww_on_device = "on"
        async with self._control(
            capabilities=["mic", "speaker", "oww_shadow", "oww_trigger"]
        ) as ctl:
            await self._handshake(ctl)
            await ctl.send(
                json.dumps({"type": "oww_shadow_cross", "score": 0.71, "ageMs": 90})
            )
            await asyncio.sleep(0.05)
            self.assertEqual(self.link.stats.shadow_crossings, 1)
            self.assertEqual(self.link.stats.wake_events, 0)
            self.assertTrue(self.link.scoring_evidence)
            self.assertEqual(self.link.drain_wakes(), [])

    async def test_oww_model_and_threshold_are_pushed_in_config(self) -> None:
        link = DeviceLink(
            tls_port=None,
            plain_port=0,
            oww_on_device="on",
            oww_model="hey_jarvis_v0.1",
            oww_threshold=0.6,
        )
        await link.start()
        port = link.bound_ports["plain"]
        self.addCleanup(link.stop)
        async with ws_client.connect(f"ws://127.0.0.1:{port}/control") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": "D090",
                        "version": "v2.19.0",
                        "capabilities": ["mic", "speaker", "oww_trigger"],
                    }
                )
            )
            messages = [json.loads(await asyncio.wait_for(ws.recv(), 2)) for _ in range(4)]
        config = next(m for m in messages if m["type"] == "config")
        self.assertEqual(config["owwModel"], "hey_jarvis_v0.1")
        self.assertAlmostEqual(config["owwThreshold"], 0.6)
        self.assertEqual(config["owwOnDevice"], "on")
        self.assertIs(config["wakeWordEnabled"], True)

    async def test_wake_during_playback_becomes_a_barge(self) -> None:
        """Stream-mode firmware has no barge bit; our playback state supplies it."""
        link = DeviceLink(
            tls_port=None,
            plain_port=0,
            oww_on_device="on",
            playing=lambda: True,
        )
        await link.start()
        port = link.bound_ports["plain"]
        self.addCleanup(link.stop)
        async with ws_client.connect(f"ws://127.0.0.1:{port}/control") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": "D090",
                        "version": "v2.19.0",
                        "capabilities": ["mic", "speaker", "oww_trigger"],
                    }
                )
            )
            for _ in range(4):
                await asyncio.wait_for(ws.recv(), 2)
            await ws.send(
                json.dumps({"type": "oww_wake", "score": 0.9, "threshold": 0.5})
            )
            await asyncio.sleep(0.05)
            events = link.drain_wakes()
        self.assertEqual(len(events), 1)
        self.assertIs(events[0].barge, True)


class AdapterWakePathTest(unittest.IsolatedAsyncioTestCase):
    """The real path: link producer -> adapter queue -> rui DeviceWake."""

    async def asyncSetUp(self) -> None:
        self.adapter = Dot2Adapter(
            "dot2",
            host="127.0.0.1",
            secrets_dir="secrets",
            oww_on_device="on",
            oww_model="hey_rui.onnx",
        )
        self.link = DeviceLink(
            tls_port=None,
            plain_port=0,
            oww_on_device="on",
            wake_word_enabled=True,
            wake_queue=self.adapter._wakes,
        )
        await self.link.start()
        self.port = self.link.bound_ports["plain"]
        self.adapter._link = self.link
        self.addCleanup(self.link.stop)

    async def test_probe_and_drain_over_a_local_scorer_link(self) -> None:
        async with ws_client.connect(f"ws://127.0.0.1:{self.port}/control") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": "D090",
                        "version": "v2.18.1",
                        "capabilities": ["mic", "speaker", "oww_local_only"],
                    }
                )
            )
            for _ in range(4):
                await asyncio.wait_for(ws.recv(), 2)
            # Capability claimed but the local scorer has not reported ownership.
            info = await self.adapter.probe()
            self.assertNotIn(Capability.DEVICE_WAKE, info.capabilities)

            await ws.send(json.dumps({"type": "listen_state", "state": "local"}))
            await asyncio.sleep(0.05)
            info = await self.adapter.probe()
            self.assertIn(Capability.DEVICE_WAKE, info.capabilities)
            self.assertIn(Capability.BARGE_VIA_DEVICE, info.capabilities)

            await ws.send(
                json.dumps(
                    {
                        "type": "oww_wake",
                        "score": 0.93,
                        "threshold": 0.5,
                        "ageMs": 80,
                        "capturedMono": True,
                        "barge": True,
                        "level": -17.0,
                    }
                )
            )
            await asyncio.sleep(0.05)
            events = self.adapter.drain_wakes()

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.source, "device")
        self.assertIs(event.barge, True)
        # Exactly the v1 shape/range/staleness rules; the kit uses this helper.
        self.assertIsNone(
            wake_validation_error(event, now_ms=time.monotonic() * 1000.0)
        )


class SendGuardTest(unittest.IsolatedAsyncioTestCase):
    """P0b: a /data peer that stops reading must not zombie the link.

    ``websockets.send()`` waits for the transport's write buffer to drain, so a
    device whose TCP receive window is closed leaves ``send_pcm`` parked forever.
    The guard is a per-frame write timeout plus an outbound watermark; either one
    aborts /data so the device re-dials. Control traffic must keep flowing while
    /data is stalled (locks are separate).
    """

    async def asyncSetUp(self) -> None:
        self.link = DeviceLink(
            tls_port=None,
            plain_port=0,
            token="secret",
            volume_cap_percent=30.0,
            push_config=True,
        )
        await self.link.start()
        self.port = self.link.bound_ports["plain"]
        self.addCleanup(self.link.stop)

    @contextlib.asynccontextmanager
    async def _control(self):
        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/control",
            additional_headers={"X-EM-Token": "secret"},
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": "D090",
                        "version": "v2.18.1",
                        "capabilities": ["mic", "speaker"],
                    }
                )
            )
            for _ in range(4):  # ack, volume_set, config, mic_start
                await asyncio.wait_for(ws.recv(), 2)
            yield ws

    @contextlib.asynccontextmanager
    async def _data(self):
        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/data",
            additional_headers={"X-EM-Token": "secret"},
        ) as ws:
            await asyncio.sleep(0.05)
            yield ws

    @contextlib.asynccontextmanager
    async def _unreading_data_socket(self):
        """A /data client that completes the handshake and then stops reading.

        The websockets client auto-drains into its own queue, so it can swallow
        tens of MB on loopback before backpressure appears. A raw socket with a
        4 KiB receive buffer is the honest "device stopped reading" peer: the
        server's send() parks after a few hundred KB and only the guard frees it.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(sock, ("127.0.0.1", self.port))
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /data HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "X-EM-Token: secret\r\n\r\n"
        ).encode()
        await loop.sock_sendall(sock, request)
        reader, writer = await asyncio.open_connection(sock=sock)
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            await asyncio.sleep(0.05)
            yield reader
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _wait_until_data_write_stalls(self, task: asyncio.Task, timeout: float = 2.0) -> None:
        """Return once send_pcm stopped making progress (i.e. it is in send())."""
        deadline = time.monotonic() + timeout
        last_frames = -1
        stable_since: float | None = None
        while time.monotonic() < deadline and not task.done():
            frames = self.link.stats.speaker_frames
            if frames == last_frames:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 0.1:
                    return
            else:
                last_frames = frames
                stable_since = None
            await asyncio.sleep(0.02)

    async def test_stalled_data_write_times_out_and_aborts(self) -> None:
        with (
            mock.patch.object(device_link_module, "WRITE_TIMEOUT_S", 0.25),
            mock.patch.object(device_link_module, "SEND_WATERMARK_BYTES", 64 * 1024 * 1024),
        ):
            async with self._control():
                async with self._unreading_data_socket():
                    started = time.monotonic()
                    ok = await asyncio.wait_for(
                        self.link.send_pcm(bytes(SPEAKER_CHUNK_BYTES * 4096)), 3
                    )
                    elapsed = time.monotonic() - started

                    self.assertIs(ok, False)
                    self.assertLess(elapsed, 2.5)
                    self.assertFalse(self.link.connected.is_set())
                    self.assertIsNone(self.link._data)

    async def test_outbound_watermark_aborts_before_any_frame(self) -> None:
        with mock.patch.object(device_link_module, "SEND_WATERMARK_BYTES", 8192):
            async with self._control():
                async with self._data() as data_ws:
                    ok = await asyncio.wait_for(
                        self.link.send_pcm(bytes(SPEAKER_CHUNK_BYTES * 3)), 2
                    )
                    self.assertIs(ok, False)
                    self.assertEqual(self.link.stats.speaker_frames, 0)
                    self.assertFalse(self.link.connected.is_set())
                    self.assertIsNone(self.link._data)
                    with self.assertRaises(ConnectionClosed):
                        await asyncio.wait_for(data_ws.recv(), 2)

    async def test_control_is_not_blocked_by_a_stalled_data_send(self) -> None:
        with (
            mock.patch.object(device_link_module, "WRITE_TIMEOUT_S", 1.0),
            mock.patch.object(device_link_module, "SEND_WATERMARK_BYTES", 64 * 1024 * 1024),
        ):
            async with self._control() as ctl:
                async with self._unreading_data_socket():
                    send_task = asyncio.create_task(
                        self.link.send_pcm(bytes(SPEAKER_CHUNK_BYTES * 4096))
                    )
                    await self._wait_until_data_write_stalls(send_task)
                    self.assertFalse(send_task.done())

                    # A shared lock would park this behind the stuck /data send
                    # (1 s timeout) and the 0.3 s bound would expire.
                    await asyncio.wait_for(self.link.set_volume_level(20), 0.3)
                    message = json.loads(await asyncio.wait_for(ctl.recv(), 0.5))
                    self.assertEqual(message["type"], "volume_set")
                    self.assertEqual(message["level"], 20)

                    self.assertIs(await asyncio.wait_for(send_task, 5), False)


class ReconnectStreamStateTest(unittest.IsolatedAsyncioTestCase):
    """P0b: a half sentence must not survive a link drop and replay later."""

    async def asyncSetUp(self) -> None:
        self.adapter = Dot2Adapter(
            "dot2",
            host="127.0.0.1",
            secrets_dir="secrets",
            volume_cap_percent=30.0,
        )
        # Start the stream on the first chunk and keep EOS out of the way so the
        # half-frame stays buffered until the link drops.
        self.adapter._prebuffer_bytes = 0
        self.adapter._eos_delay = lambda: 5.0  # type: ignore[method-assign]
        self.adapter._loudness.process_bytes = lambda pcm: pcm  # type: ignore[method-assign]
        self.adapter._loudness.set_level = lambda level: None  # type: ignore[method-assign]
        self.link = DeviceLink(
            tls_port=None,
            plain_port=0,
            token="secret",
            volume_cap_percent=30.0,
        )
        # The adapter passes this as a constructor kwarg in open(); wiring that
        # path is covered by test_adapter_hands_the_link_loss_callback_down.
        self.link.on_link_lost = self.adapter._on_link_lost
        await self.link.start()
        self.port = self.link.bound_ports["plain"]
        self.adapter._link = self.link
        self.addCleanup(self.link.stop)

    @contextlib.asynccontextmanager
    async def _control(self):
        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/control",
            additional_headers={"X-EM-Token": "secret"},
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": "D090",
                        "version": "v2.18.1",
                        "capabilities": ["mic", "speaker"],
                    }
                )
            )
            for _ in range(4):
                await asyncio.wait_for(ws.recv(), 2)
            yield ws

    @contextlib.asynccontextmanager
    async def _data(self):
        async with ws_client.connect(
            f"ws://127.0.0.1:{self.port}/data",
            additional_headers={"X-EM-Token": "secret"},
        ) as ws:
            await asyncio.sleep(0.05)
            yield ws

    async def _wait_until(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail("condition not reached in time")

    async def test_reconnect_does_not_replay_a_half_stream(self) -> None:
        async with self._control():
            async with self._data() as data1:
                half = b"\xAA" * (SPEAKER_CHUNK_BYTES + 1000)
                await self.adapter.play(half, sample_rate=48000, channels=1)
                first = await asyncio.wait_for(data1.recv(), 2)
                self.assertEqual(first[0], FRAME_SPEAKER)
                self.assertEqual(first[1:], b"\xAA" * SPEAKER_CHUNK_BYTES)
                self.assertTrue(self.adapter._playing)
                self.assertEqual(len(self.adapter._frame_buffer), 1000)

            await self._wait_until(lambda: self.link._data is None)
            self.assertFalse(self.link.connected.is_set())
            self.assertFalse(self.adapter._playing)
            self.assertFalse(self.adapter._stream_started)
            self.assertEqual(self.adapter._stream_bytes, 0)
            self.assertEqual(bytes(self.adapter._frame_buffer), b"")

            async with self._data() as data2:
                await self._wait_until(lambda: self.link.connected.is_set())
                # A whole frame of the new reply: the stale 1000-byte tail must
                # not be prepended to it (that would replay the old sentence).
                await self.adapter.play(
                    b"\xBB" * SPEAKER_CHUNK_BYTES, sample_rate=48000, channels=1
                )
                frame = await asyncio.wait_for(data2.recv(), 2)
                self.assertEqual(frame[0], FRAME_SPEAKER)
                self.assertEqual(frame[1:], b"\xBB" * SPEAKER_CHUNK_BYTES)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(data2.recv(), 0.2)

    def test_adapter_hands_the_link_loss_callback_down(self) -> None:
        adapter = Dot2Adapter("dot2", host="127.0.0.1", secrets_dir="secrets")
        callback = adapter._link_kwargs["on_link_lost"]
        self.assertIs(callback.__self__, adapter)
        self.assertEqual(callback, adapter._on_link_lost)


class AbnormalCloseResetTest(unittest.IsolatedAsyncioTestCase):
    """P0b: an abnormal close must leave no slot the next register cannot use.

    Field evidence (2026-10-28 12:1x): after a power cut the device loops
    ``connection open`` -> exactly HANDSHAKE_TIMEOUT_S -> ``connection closed``
    and never sends register, while restarting the bridge (fresh state) recovers
    in 3 s. The bridge is a server with one device slot, so the state a previous
    connection left behind is the only thing a restart can clear: it must be
    cleared on every abnormal close instead.
    """

    async def asyncSetUp(self) -> None:
        self.link = DeviceLink(
            tls_port=None,
            plain_port=0,
            token="secret",
            volume_cap_percent=30.0,
            push_config=True,
        )
        await self.link.start()
        self.port = self.link.bound_ports["plain"]
        self.addCleanup(self.link.stop)

    # ------------------------------------------------------------- harness
    async def _open_silent_control(self):
        """A device that completes TLS/WS and then never sends register."""
        return await ws_client.connect(
            f"ws://127.0.0.1:{self.port}/control",
            additional_headers={"X-EM-Token": "secret"},
        )

    async def _register_control(self, device_id: str = "D090"):
        """A device that dials /control and completes the register handshake."""
        ws = await self._open_silent_control()
        await ws.send(
            json.dumps(
                {
                    "type": "register",
                    "device_id": device_id,
                    "version": "v2.18.1",
                    "capabilities": ["mic", "speaker"],
                }
            )
        )
        for _ in range(4):  # ack, volume_set, config, mic_start
            await asyncio.wait_for(ws.recv(), 2)
        return ws

    async def _open_data(self):
        ws = await ws_client.connect(
            f"ws://127.0.0.1:{self.port}/data",
            additional_headers={"X-EM-Token": "secret"},
        )
        await asyncio.sleep(0.05)
        return ws

    async def _wait_until(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail("condition not reached in time")

    async def _expect_closed(self, ws, timeout: float = 3.0) -> None:
        """Drain whatever is queued until the bridge drops the socket."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(ws.recv(), max(0.01, deadline - time.monotonic()))
            except ConnectionClosed:
                return
            except TimeoutError:
                break
        self.fail("bridge did not close the socket")

    # --------------------------------------------------------------- tests
    async def test_register_after_a_silent_connection_times_out(self) -> None:
        """connect -> device silent -> force close -> reconnect + register.

        This is the observed field loop. Pre-fix the handshake timeout closes
        /control without a word and leaves the /data slot pointing at the dead
        incarnation, so the next register has no clean link to land on.
        """
        with mock.patch.object(device_link_module, "HANDSHAKE_TIMEOUT_S", 0.3):
            with self.assertLogs("rui_dot2_bridge.device_link", level="WARNING") as captured:
                stale_data = await self._open_data()
                silent = await self._open_silent_control()
                await self._expect_closed(silent)
                # The link as a whole is gone: the /data slot and the gate must
                # not outlive the control handshake.
                await self._wait_until(lambda: self.link._data is None)
                self.assertFalse(self.link.connected.is_set())
                await self._expect_closed(stale_data)

            # The field symptom was a loop: a second silent attempt must not
            # accumulate a slot, a meter, a waiter or a ping task either.
            with self.assertLogs("rui_dot2_bridge.device_link", level="WARNING") as second:
                silent_again = await self._open_silent_control()
                await self._expect_closed(silent_again)
                await self._wait_until(lambda: self.link._data is None)
            self.assertIn("register did not arrive", "\n".join(second.output))

        timeout_log = "\n".join(captured.output)
        self.assertIn("register did not arrive", timeout_log)
        # Nothing from the failed attempts survives to the real registration.
        self.assertEqual(self.link._meters, {})
        self.assertEqual(self.link._pong_waiters, {})
        self.assertEqual(self.link._pong_events, {})
        self.assertIsNone(self.link._ping_task)

        # Same device, fresh attempt: register must work, and playback must
        # reach the socket the device dialled after the cut.
        control = await self._register_control()
        self.assertIsNotNone(self.link._control)
        self.assertTrue(self.link.connected.is_set())
        self.assertIsNotNone(self.link._ping_task)
        self.assertFalse(self.link._ping_task.done())

        data = await self._open_data()
        await self._wait_until(lambda: self.link._data is not None)
        self.assertIs(await asyncio.wait_for(self.link.send_pcm(bytes(SPEAKER_CHUNK_BYTES)), 2), True)
        frame = await asyncio.wait_for(data.recv(), 2)
        self.assertEqual(frame[0], FRAME_SPEAKER)
        await control.close()
        await data.close()

    async def test_a_superseded_control_cannot_tear_down_the_live_register(self) -> None:
        """A reconnect can precede the detection of the old socket's death.

        The stale handler's cleanup must only touch its own connection: the live
        registration keeps its slot, its gate and its ping loop.
        """
        with mock.patch.object(device_link_module, "PING_INTERVAL_S", 0.05):
            stale = await self._register_control()
            live = await self._register_control()
            registered = self.link._control
            self.assertIsNotNone(registered)

            await self._expect_closed(stale)
            await asyncio.sleep(0.1)  # let the stale handler run its finally
            # Same server-side connection: the stale handler may only clean up
            # after itself.
            self.assertIs(self.link._control, registered)
            self.assertTrue(self.link.connected.is_set())
            ping_task = self.link._ping_task
            self.assertIsNotNone(ping_task)
            self.assertFalse(ping_task.done())

            # The live connection must still be probed: a reused registration
            # whose ping loop was cancelled by the old handler is half dead.
            message = None
            for _ in range(10):
                message = json.loads(await asyncio.wait_for(live.recv(), 2))
                if message["type"] == "ping":
                    break
            self.assertEqual(message["type"], "ping")
            await live.send(json.dumps({"type": "pong", "id": message["id"]}))
            await self._wait_until(lambda: self.link.stats.rtt_ms is not None)

            await asyncio.wait_for(self.link.set_volume_level(15), 1)
            # Pings keep interleaving on the 50 ms interval; look for the order.
            for _ in range(10):
                message = json.loads(await asyncio.wait_for(live.recv(), 1))
                if message["type"] == "volume_set":
                    break
            self.assertEqual(message, {"type": "volume_set", "level": 15})
            await live.close()

    async def test_a_blocked_control_write_cannot_outlive_its_bound(self) -> None:
        """P0b: the next register's ack waits on this lock, so a stale write is bounded.

        A sender parked on the previous socket used to hold the control lock
        forever, which is a stale queue a reconnect cannot get past.
        """
        control = await self._register_control()
        lost: list[str] = []
        self.link.on_link_lost = lost.append
        poisoned = self.link._control_lock
        with mock.patch.object(device_link_module, "CONTROL_WRITE_TIMEOUT_S", 0.2):
            await poisoned.acquire()
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                await self.link.set_volume_level(20)
            self.assertLess(time.monotonic() - started, 2.0)

        self.assertIsNone(self.link._control)
        self.assertFalse(self.link.connected.is_set())
        self.assertIsNot(self.link._control_lock, poisoned)
        self.assertTrue(lost)
        # Its probe loop went with the plane: nothing is left to probe.
        self.assertIsNone(self.link._ping_task)

        # The device reconnects: the abandoned lock must not delay its ack.
        again = await self._register_control()
        self.assertIsNotNone(self.link._control)
        self.assertTrue(self.link.connected.is_set())
        await control.close()
        await again.close()

    async def test_abnormal_data_close_logs_code_direction_and_bytes(self) -> None:
        """P2: the close line must name the code, the plane and the bytes seen."""
        control = await self._register_control()
        registered = self.link._control
        data = await self._open_data()
        await self._wait_until(lambda: self.link.connected.is_set())
        self.assertIs(await asyncio.wait_for(self.link.send_pcm(bytes(SPEAKER_CHUNK_BYTES)), 2), True)
        await asyncio.wait_for(data.recv(), 2)

        lost: list[str] = []
        self.link.on_link_lost = lost.append
        with self.assertLogs("rui_dot2_bridge.device_link", level="WARNING") as captured:
            data.transport.abort()
            await self._wait_until(lambda: self.link._data is None)

        close_log = "\n".join(captured.output)
        self.assertIn("dir=data", close_log)
        self.assertIn("code=", close_log)
        self.assertIn("rx=", close_log)
        self.assertEqual(self.link._inflight_bytes, 0)
        self.assertTrue(lost, "the adapter must be told the link went away")
        # Control survived: the device is still registered after losing /data.
        self.assertIs(self.link._control, registered)
        self.assertIsNotNone(self.link._ping_task)
        self.assertFalse(self.link._ping_task.done())

        # A reconnect on /data must be usable again.
        data2 = await self._open_data()
        await self._wait_until(lambda: self.link.connected.is_set())
        self.assertIs(await asyncio.wait_for(self.link.send_pcm(bytes(SPEAKER_CHUNK_BYTES)), 2), True)
        self.assertEqual((await asyncio.wait_for(data2.recv(), 2))[0], FRAME_SPEAKER)
        await control.close()
        await data2.close()


class VolumeMathTest(unittest.TestCase):
    def test_cap_holds_against_requests(self) -> None:
        from rui_dot2_bridge.volume import clamp_percent, percent_to_level

        self.assertEqual(clamp_percent(100.0, 30.0), 30.0)
        self.assertEqual(percent_to_level(30.0), 38)
        self.assertEqual(percent_to_level(0.0), 0)

    def test_nan_becomes_silence_not_full(self) -> None:
        from rui_dot2_bridge.volume import clamp_percent

        self.assertEqual(clamp_percent(float("nan")), 0.0)


if __name__ == "__main__":
    unittest.main()
