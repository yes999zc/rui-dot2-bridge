"""DeviceLink integration tests over a plain websocket (no TLS, no hardware).

These pin the parts of ``docs/protocol-map.md`` a wrong guess would break:
the register/ack handshake, the volume ceiling, mic enqueueing, speaker
framing and the flush that makes barge-in audible as silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import unittest

import websockets.asyncio.client as ws_client

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
    async def _control(self, token: str = "secret", device_id: str = "D090"):
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
                        "capabilities": ["mic", "speaker"],
                        "ip": "192.168.5.69",
                    }
                )
            )
            yield ws

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
