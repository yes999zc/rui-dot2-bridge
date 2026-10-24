"""Adapter unit tests with a fake link: no device, no TLS, no rui plane.

They pin the contract the plane relies on: honest capabilities, 20 ms frames,
whole 4096-byte speaker periods, an EOS after the reply goes quiet, and a hard
30 % volume ceiling.
"""

from __future__ import annotations

import asyncio
import unittest

from rui_dot2_bridge.adapter import Dot2Adapter
from rui_dot2_bridge.frames import SPEAKER_CHUNK_BYTES
from rui_dot2_bridge.volume import capped_level


class FakeInfo:
    device_id = "dot2"
    version = "v2.18.1"
    ip = "<lan-ip>"


class FakeStats:
    data_connects = 1
    rtt_ms = 5.0
    mic_frames = 0
    dropped_audio = 0


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
        host="<lan-ip>",
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
        from house_ear.device.contract import Capability

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

    async def test_stop_playback_flushes_and_drops_buffer(self) -> None:
        await self.adapter.play(bytes(5000), sample_rate=48000, channels=1)
        await self.adapter.stop_playback()
        self.assertEqual(self.link.flushed, 1)
        await asyncio.sleep(1.5)
        self.assertEqual(self.link.eos, 0)  # the idle EOS must not fire after a stop


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


if __name__ == "__main__":
    unittest.main()
