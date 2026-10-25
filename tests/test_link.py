"""DeviceLink integration tests over a plain websocket (no TLS, no hardware).

These pin the parts of ``docs/protocol-map.md`` a wrong guess would break:
the register/ack handshake, the volume ceiling, mic enqueueing, speaker
framing and the flush that makes barge-in audible as silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import unittest

import websockets.asyncio.client as ws_client
from rui.device.contract import Capability, wake_validation_error

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
                        "ip": "192.168.5.69",
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
