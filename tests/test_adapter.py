"""Adapter unit tests with a fake link: no device, no TLS, no rui plane.

They pin the contract the plane relies on: honest capabilities, 20 ms frames,
whole 4096-byte speaker periods, an EOS after the reply goes quiet, and a hard
30 % volume ceiling.
"""

from __future__ import annotations

import asyncio
import unittest

from rui.device.contract import Capability

from rui_dot2_bridge.adapter import Dot2Adapter
from rui_dot2_bridge.frames import SPEAKER_CHUNK_BYTES
from rui_dot2_bridge.volume import capped_level


class FakeInfo:
    device_id = "dot2"
    version = "v2.18.1"
    ip = "192.168.5.69"


class FakeStats:
    data_connects = 1
    rtt_ms = 5.0
    mic_frames = 0
    dropped_audio = 0
    wake_events = 0
    invalid_wakes = 0


class FakeLink:
    def __init__(self, blocks: list[bytes] | None = None) -> None:
        self.blocks = blocks or []
        self.sent: list[bytes] = []
        self.eos = 0
        self.flushed = 0
        self.volumes: list[float] = []
        self.leds: list[tuple[list[dict], bool]] = []
        self.stopped = 0
        self.connected = asyncio.Event()
        self.connected.set()
        self.stats = FakeStats()
        self.info = FakeInfo()
        self.volume_level = 38
        self.reported_level = 38
        self.volume_cap_percent = 30.0
        self.aec_enabled = True
        # Live-link proof of on-device wake scoring; probe() gates on this.
        self.device_wake_available = False

    async def frames(self):
        for block in self.blocks:
            yield block
            await asyncio.sleep(0)

    async def send_pcm(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def end_stream(self) -> None:
        self.eos += 1

    async def flush(self) -> None:
        self.flushed += 1

    async def set_volume_percent(self, percent: float) -> None:
        self.volumes.append(percent)

    async def set_leds(self, leds: list[dict], listening: bool = False) -> None:
        self.leds.append((leds, listening))

    async def stop(self) -> None:
        self.stopped += 1


def make_adapter(**kwargs) -> Dot2Adapter:
    adapter = Dot2Adapter(
        "dot2",
        host="192.168.5.18",
        secrets_dir="secrets",
        **kwargs,
    )
    adapter._link = FakeLink(kwargs.pop("_blocks", []))
    return adapter


async def collect_frames(adapter: Dot2Adapter) -> list:
    frames = []
    async for frame in adapter.frames():
        frames.append(frame)
    return frames


class CaptureLayoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_mono_block_becomes_four_20ms_frames(self) -> None:
        adapter = make_adapter()
        adapter._link.blocks = [bytes(2560)]  # 80 ms mono S16
        frames = await collect_frames(adapter)
        self.assertEqual(len(frames), 4)
        self.assertTrue(all(len(f.pcm) == 640 for f in frames))  # 20 ms * 16 kHz * 2 B
        self.assertTrue(all((f.channels, f.reference_channels) == (1, 0) for f in frames))
        self.assertEqual(frames[0].sample_rate, 16000)

    async def test_two_channel_layout_keeps_interleaving(self) -> None:
        adapter = make_adapter(mic_channels=1, ref_channels=1)
        adapter._link.blocks = [bytes(5120)]  # 80 ms of 2 interleaved channels
        frames = await collect_frames(adapter)
        self.assertEqual(len(frames), 4)
        self.assertTrue(all(len(f.pcm) == 1280 for f in frames))
        self.assertTrue(all((f.channels, f.reference_channels) == (2, 1) for f in frames))
        self.assertEqual(frames[0].mic_channels, 1)

    async def test_capabilities_stay_honest(self) -> None:
        mono = make_adapter()
        self.assertNotIn(Capability.REFERENCE, mono.info.capabilities)
        self.assertNotIn(Capability.MIC_ARRAY, mono.info.capabilities)
        stereo = make_adapter(mic_channels=1, ref_channels=1)
        self.assertIn(Capability.REFERENCE, stereo.info.capabilities)
        self.assertNotIn(Capability.MIC_ARRAY, stereo.info.capabilities)


class PlaybackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = make_adapter()
        self.link: FakeLink = self.adapter._link  # type: ignore[assignment]

    async def test_play_sends_whole_periods_only(self) -> None:
        self.adapter._prebuffer_bytes = 0  # send immediately for this framing test
        await self.adapter.play(bytes(5000), sample_rate=48000, channels=1)
        self.assertEqual([len(b) for b in self.link.sent], [SPEAKER_CHUNK_BYTES])
        await self.adapter.play(bytes(4000), sample_rate=48000, channels=1)
        self.assertEqual([len(b) for b in self.link.sent], [SPEAKER_CHUNK_BYTES] * 2)

    async def test_rejects_wrong_rate_or_channels(self) -> None:
        with self.assertRaises(ValueError):
            await self.adapter.play(bytes(100), sample_rate=16000, channels=1)
        with self.assertRaises(ValueError):
            await self.adapter.play(bytes(100), sample_rate=48000, channels=2)

    async def test_idle_sends_padded_tail_and_eos(self) -> None:
        await self.adapter.play(bytes(5000), sample_rate=48000, channels=1)
        await asyncio.sleep(1.5)  # EOS_IDLE_S is 1.2
        self.assertEqual(self.link.eos, 1)
        self.assertEqual(len(self.link.sent), 2)
        self.assertEqual(len(self.link.sent[-1]), SPEAKER_CHUNK_BYTES)  # tail padded
        self.assertEqual(self.link.sent[-1][-2:], b"\x00\x00")  # faded to silence

    async def test_stop_playback_when_idle_still_flushes(self) -> None:
        # The flush must go out even when idle: it opens the device's 1.024 s prime
        # bypass. DeviceLink.flush() adds the pong barrier that orders it before
        # the first PCM block (the old 406 ms hole).
        await self.adapter.stop_playback()
        self.assertEqual(self.link.flushed, 1)
        self.assertEqual(self.link.eos, 0)
        self.assertEqual(len(self.link.sent), 0)

    async def test_prebuffer_holds_audio_until_enough_arrives(self) -> None:
        await self.adapter.play(bytes(9600), sample_rate=48000, channels=1)
        self.assertEqual(self.link.sent, [])  # 100 ms < 450 ms lead
        await self.adapter.play(bytes(SPEAKER_CHUNK_BYTES * 12), sample_rate=48000, channels=1)
        self.assertTrue(self.link.sent)  # crossed the threshold and started

    async def test_stop_playback_while_active_flushes_then_ends_the_stream(self) -> None:
        self.adapter._prebuffer_bytes = 0
        await self.adapter.play(bytes(9000), sample_rate=48000, channels=1)
        await self.adapter.stop_playback()
        self.assertEqual(self.link.flushed, 1)
        # EOS after the flush clears the device's discarding state, otherwise the
        # next reply's first frames can be swallowed.
        self.assertEqual(self.link.eos, 1)

    async def test_stop_playback_flushes_and_drops_buffer(self) -> None:
        await self.adapter.play(bytes(5000), sample_rate=48000, channels=1)
        await self.adapter.stop_playback()
        self.assertEqual(self.link.flushed, 1)
        self.assertEqual(self.link.eos, 1)  # active stop ends the stream explicitly
        await asyncio.sleep(1.5)
        self.assertEqual(self.link.eos, 1)  # the idle EOS must not fire again


class VolumeTest(unittest.IsolatedAsyncioTestCase):
    async def test_volume_ceiling_applies_to_plane_requests(self) -> None:
        adapter = make_adapter()
        link: FakeLink = adapter._link  # type: ignore[assignment]
        await adapter.set_volume(100)
        await adapter.set_volume(10)
        self.assertEqual(link.volumes, [30.0, 10.0])
        self.assertLessEqual(link.volumes[0], 30.0)

    async def test_health_reports_connection_and_profile(self) -> None:
        adapter = make_adapter()
        health = adapter.health()
        self.assertTrue(health["connected"])
        self.assertEqual(health["device_id"], "dot2")
        self.assertIn("1 mic + 0 ref", health["profile"])


class LedTest(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_paints_all_leds(self) -> None:
        adapter = make_adapter()
        link: FakeLink = adapter._link  # type: ignore[assignment]
        await adapter.set_led("thinking")
        leds, listening = link.leds[-1]
        self.assertEqual(len(leds), 12)
        self.assertEqual([led["id"] for led in leds], list(range(12)))
        self.assertEqual((leds[0]["r"], leds[0]["g"], leds[0]["b"]), (0, 40, 160))
        self.assertFalse(listening)

    async def test_wake_marks_the_ring_listening(self) -> None:
        adapter = make_adapter()
        link: FakeLink = adapter._link  # type: ignore[assignment]
        await adapter.set_led("wake")
        leds, listening = link.leds[-1]
        self.assertTrue(listening)
        self.assertEqual((leds[0]["r"], leds[0]["g"], leds[0]["b"]), (0, 150, 70))

    async def test_unknown_state_goes_dark(self) -> None:
        adapter = make_adapter()
        link: FakeLink = adapter._link  # type: ignore[assignment]
        await adapter.set_led("nonsense")
        leds, _ = link.leds[-1]
        self.assertEqual((leds[0]["r"], leds[0]["g"], leds[0]["b"]), (0, 0, 0))


class RefUplinkTest(unittest.IsolatedAsyncioTestCase):
    def test_two_channel_capture_announces_the_feature(self) -> None:
        adapter = make_adapter(mic_channels=1, ref_channels=1)
        self.assertEqual(adapter._link_kwargs["ref_uplink"], True)

    def test_mono_capture_announces_nothing(self) -> None:
        adapter = make_adapter()
        self.assertEqual(adapter._link_kwargs["ref_uplink"], False)


class Volume255Test(unittest.IsolatedAsyncioTestCase):
    async def test_at_ceiling_the_projection_matches_config(self) -> None:
        adapter = make_adapter()
        # Both sides use Python's round(): round(30 * 255 / 100) == 76, and the
        # value only has to agree exactly with what rui's reconciler computes.
        self.assertEqual(adapter.volume_255(), round(30 * 255 / 100))

    async def test_services_set_volume_is_clamped_to_the_ceiling(self) -> None:
        adapter = make_adapter()
        link: FakeLink = adapter._link  # type: ignore[assignment]
        await adapter.services.set_volume(200)  # loud console request
        self.assertEqual(link.volumes[-1], 30.0)  # never above the cap
        await adapter.services.set_volume(77)
        self.assertEqual(link.volumes[-1], 30.0)

    async def test_read_volume_returns_the_255_projection(self) -> None:
        adapter = make_adapter()
        self.assertEqual(await adapter.read_volume(), round(30 * 255 / 100))

    async def test_high_cap_does_not_report_a_low_device_as_full(self) -> None:
        # Regression: capped_level() was called without the cap, so with cap=100 a
        # device still at level 38 looked like "at the ceiling" and reported 255.
        adapter = make_adapter(volume_cap_percent=100)
        link: FakeLink = adapter._link  # type: ignore[assignment]
        link.reported_level = 38
        self.assertEqual(adapter.volume_255(), 76)
        link.reported_level = 89
        self.assertEqual(adapter.volume_255(), round(89 * 255 / 127))
        link.reported_level = 127
        self.assertEqual(adapter.volume_255(), 255)


class CeilingTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_ceiling_is_not_reclamped_to_the_default(self) -> None:
        adapter = make_adapter(volume_cap_percent=70)
        self.assertEqual(adapter.volume_cap_percent, 70.0)
        link: FakeLink = adapter._link  # type: ignore[assignment]
        await adapter.set_volume(100)
        self.assertEqual(link.volumes[-1], 70.0)  # capped at 70, not at 30

    async def test_seventy_percent_maps_to_level_89(self) -> None:
        from rui_dot2_bridge.volume import percent_to_level

        self.assertEqual(percent_to_level(70), 89)


class VolumeMappingTest(unittest.TestCase):
    def test_cap(self) -> None:
        self.assertEqual(capped_level(100), 38)
        self.assertEqual(capped_level(0), 0)


class WakeContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_drain_wakes_returns_an_injected_event_once(self) -> None:
        adapter = make_adapter()
        adapter.inject_wake(ts_ms=1234.0, score=0.9, threshold=0.5, level=-30.0)
        events = adapter.drain_wakes()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.ts_ms, 1234.0)
        self.assertAlmostEqual(event.score, 0.9)
        self.assertEqual(event.source, "device")
        self.assertIs(event.barge, False)
        self.assertEqual(adapter.drain_wakes(), [])

    async def test_queue_is_bounded_and_drops_the_oldest(self) -> None:
        adapter = make_adapter()
        for index in range(40):
            adapter.inject_wake(ts_ms=float(index))
        self.assertEqual(len(adapter._wakes), 32)
        self.assertEqual(adapter._wakes.dropped, 8)
        events = adapter.drain_wakes()
        self.assertEqual(len(events), 32)
        self.assertEqual(events[0].ts_ms, 8.0)  # the eight oldest were dropped
        self.assertEqual(events[-1].ts_ms, 39.0)

    async def test_invalid_barge_type_is_rejected(self) -> None:
        adapter = make_adapter()
        with self.assertRaises(TypeError):
            adapter.inject_wake(barge="false")  # type: ignore[arg-type]

    async def test_probe_only_advertises_device_wake_with_live_proof(self) -> None:
        adapter = make_adapter(oww_on_device="on", oww_model="hey_rui.onnx")
        self.assertNotIn(Capability.DEVICE_WAKE, (await adapter.probe()).capabilities)
        adapter._link.device_wake_available = True
        info = await adapter.probe()
        self.assertIn(Capability.DEVICE_WAKE, info.capabilities)
        self.assertIn(Capability.BARGE_VIA_DEVICE, info.capabilities)

    async def test_drain_signals_stays_empty_and_separate(self) -> None:
        adapter = make_adapter()
        adapter.inject_wake()
        self.assertEqual(adapter.drain_signals(), [])
        self.assertEqual(len(adapter.drain_wakes()), 1)

    async def test_close_stops_returning_wakes(self) -> None:
        adapter = make_adapter()
        adapter.inject_wake()
        await adapter.close()
        self.assertEqual(adapter.drain_wakes(), [])
        adapter.inject_wake()
        self.assertEqual(adapter.drain_wakes(), [])


class VolumeUnitsTest(unittest.IsolatedAsyncioTestCase):
    async def test_services_reject_out_of_range_instead_of_clamping(self) -> None:
        adapter = make_adapter()
        for value in (-1, 256, True, 1.5):
            with self.assertRaises(ValueError):
                await adapter.services.set_volume(value)  # type: ignore[arg-type]

    async def test_high_level_set_volume_rejects_out_of_range(self) -> None:
        adapter = make_adapter()
        for value in (-1, 101, True):
            with self.assertRaises(ValueError):
                await adapter.set_volume(value)  # type: ignore[arg-type]

    async def test_services_255_maps_to_the_full_high_level_range(self) -> None:
        adapter = make_adapter(volume_cap_percent=100)
        link: FakeLink = adapter._link  # type: ignore[assignment]
        await adapter.services.set_volume(255)
        self.assertEqual(link.volumes[-1], 100.0)
        await adapter.services.set_volume(0)
        self.assertEqual(link.volumes[-1], 0.0)
        await adapter.set_volume(50)
        self.assertEqual(link.volumes[-1], 50.0)


class PlaybackSilenceWindowTest(unittest.TestCase):
    """Two windows: live during/just after playback, muted only after it.

    Barge-in needs the microphone while the Dot is speaking, but RUI's follow-up
    window (~1 s after the last block) would otherwise transcribe the residual
    echo into a turn. The gate therefore starts after PLAYBACK_LIVE_S, not at 0.
    """

    def _adapter(self, *, gate_s: float = 2.0, live_s: float = 0.35,
                 last: float | None = 100.0) -> Dot2Adapter:
        adapter = make_adapter()
        adapter._playback_gate_s = gate_s
        adapter._playback_live_s = live_s
        adapter._last_play_at = last
        return adapter

    def test_open_during_playback_so_barge_works(self) -> None:
        adapter = self._adapter()
        self.assertFalse(adapter._silence_window_active(100.0))
        self.assertFalse(adapter._silence_window_active(100.02))

    def test_open_for_the_device_drain_tail(self) -> None:
        adapter = self._adapter()
        self.assertFalse(adapter._silence_window_active(100.2))

    def test_muted_inside_the_post_playback_window(self) -> None:
        adapter = self._adapter()
        self.assertTrue(adapter._silence_window_active(100.4))
        self.assertTrue(adapter._silence_window_active(101.5))
        self.assertTrue(adapter._silence_window_active(102.0))  # inclusive edge

    def test_open_again_after_the_window(self) -> None:
        adapter = self._adapter()
        self.assertFalse(adapter._silence_window_active(102.01))

    def test_disabled_when_gate_is_zero_or_never_played(self) -> None:
        self.assertFalse(self._adapter(gate_s=0.0)._silence_window_active(100.5))
        self.assertFalse(self._adapter(last=None)._silence_window_active(100.5))

    def test_gate_mic_keeps_the_reference_channels(self) -> None:
        adapter = self._adapter()
        adapter.ref_channels = 1
        adapter.channels = 2
        silenced = adapter._gate_mic(b"\x01\x02\x03\x04")
        self.assertEqual(silenced[:2], b"\x00\x00")
        self.assertEqual(silenced[2:], b"\x03\x04")


if __name__ == "__main__":
    unittest.main()
