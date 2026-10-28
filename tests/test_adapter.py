"""Adapter unit tests with a fake link: no device, no TLS, no rui plane.

They pin the contract the plane relies on: honest capabilities, 20 ms frames,
whole 4096-byte speaker periods, an EOS after the reply goes quiet, and a hard
30 % volume ceiling.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from unittest import mock

import websockets.asyncio.client as ws_client
from rui.device.contract import Capability

import rui_dot2_bridge.adapter as adapter_module
from rui_dot2_bridge.adapter import Dot2Adapter
from rui_dot2_bridge.device_link import DeviceLink
from rui_dot2_bridge.frames import FRAME_MIC, SPEAKER_CHUNK_BYTES
from rui_dot2_bridge.volume import capped_level


class FakeInfo:
    device_id = "dot2"
    version = "v2.18.1"
    ip = "<lan-ip>"


class FakeStats:
    def __init__(self) -> None:
        self.data_connects = 1
        self.rtt_ms = 5.0
        self.mic_frames = 0
        self.dropped_audio = 0
        self.wake_events = 0
        self.invalid_wakes = 0
        self.half_open_drops = 0
        # A fake link models a live microphone by default: health() requires a
        # data-plane sample fresher than SPEAKER_STALE_S (P0b-C2).
        self.last_mic_at = time.monotonic()
        self.last_pcm_sent_at = 0.0


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
        self.data_plane_up = True
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

    async def test_short_stream_plays_a_windowed_tail_promptly(self) -> None:
        # 5000 B = one 4096 B period + 904 B (10.6 ms) tail, and the whole stream
        # is a short cue: cutting the period would click on the last syllable
        # ("在呢", 2026-10-25), so the tail goes out windowed after a short idle.
        await self.adapter.play(b"\x10\x20" * 2500, sample_rate=48000, channels=1)
        await asyncio.sleep(0.8)  # short streams use SHORT_STREAM_EOS_IDLE_S=0.15
        self.assertEqual(self.link.eos, 1)
        self.assertEqual(len(self.link.sent), 2)
        self.assertEqual(len(self.link.sent[-1]), SPEAKER_CHUNK_BYTES)  # padded
        self.assertEqual(self.link.sent[-1][-2:], b"\x00\x00")  # faded out
        self.assertNotEqual(self.link.sent[-1][:2], b"\x00\x00")  # continuous onset

    async def test_long_stream_drops_a_short_tail_instead_of_playing_it_late(self) -> None:
        # A 1.5 s+ stream ending with a 904 B tail: the word is long over, so the
        # fragment is dropped rather than played one idle later.
        self.adapter._prebuffer_bytes = 0
        await self.adapter.play(bytes(SPEAKER_CHUNK_BYTES * 36 + 904),
                                sample_rate=48000, channels=1)
        await asyncio.sleep(1.5)  # long streams keep EOS_IDLE_S=1.2
        self.assertEqual(self.link.eos, 1)
        self.assertEqual(len(self.link.sent), 36)  # no fragment, no ghost stream

    async def test_idle_fades_a_long_tail_and_pads_it(self) -> None:
        # 4096 + 3000 B (31.25 ms) is longer than the drop threshold: it is owed
        # to the listener and must go out padded to a whole period, faded on both
        # it keeps its audio onset (the stream continues) and only its end is faded.
        await self.adapter.play(b"\x10\x20" * 3548, sample_rate=48000, channels=1)
        await asyncio.sleep(1.5)
        self.assertEqual(self.link.eos, 1)
        self.assertEqual(len(self.link.sent), 2)
        self.assertEqual(len(self.link.sent[-1]), SPEAKER_CHUNK_BYTES)  # padded
        self.assertEqual(self.link.sent[-1][-2:], b"\x00\x00")  # faded out
        self.assertNotEqual(self.link.sent[-1][:2], b"\x00\x00")  # continuous onset

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


class HealthFreshnessTest(unittest.IsolatedAsyncioTestCase):
    """P0b-C2: `connected` is a data-plane fact, not a sticky socket flag.

    The dot2 /data socket can stay open while the device is gone (power cut,
    firmware hang); `data_connects > 0` then says "it connected once", which is
    what made `speaker_connected` stick for 887 s in the field. Health therefore
    needs a recent received mic frame or a recent successful PCM write.
    """

    def _adapter(self) -> Dot2Adapter:
        adapter = make_adapter()
        return adapter

    def test_a_silent_data_plane_goes_disconnected_after_the_stale_window(self) -> None:
        adapter = self._adapter()
        link = adapter._link
        assert adapter.health()["connected"] is True

        with mock.patch.object(adapter_module, "SPEAKER_STALE_S", 0.3):
            # Power cut: the socket still looks open, but nothing arrives/sends.
            link.stats.last_mic_at = time.monotonic() - 0.5
            link.stats.last_pcm_sent_at = 0.0
            health = adapter.health()
            self.assertFalse(health["connected"])
            self.assertFalse(health["speaker_fresh"])
            self.assertIsNotNone(health["data_idle_ms"])

            # A single received mic frame proves the plane again.
            link.stats.last_mic_at = time.monotonic()
            self.assertTrue(adapter.health()["connected"])

            # ...and so does a successfully written PCM frame on an idle mic.
            link.stats.last_mic_at = time.monotonic() - 0.5
            link.stats.last_pcm_sent_at = time.monotonic()
            self.assertTrue(adapter.health()["connected"])

    def test_no_data_plane_or_a_cleared_gate_is_disconnected_even_when_fresh(self) -> None:
        adapter = self._adapter()
        link = adapter._link
        self.assertTrue(adapter.health()["connected"])

        link.data_plane_up = False
        health = adapter.health()
        self.assertFalse(health["connected"])
        self.assertFalse(health["data_plane"])

        link.data_plane_up = True
        link.connected.clear()
        self.assertFalse(adapter.health()["connected"])


class AdapterLinkFreshnessTest(unittest.IsolatedAsyncioTestCase):
    """P0b-C2 over a real DeviceLink: power cut flips health, reconnect restores it.

    The fake-link tests above pin the arithmetic; this one pins the wiring that
    the house actually sees: a real `DeviceLink` feeds `stats.last_mic_at`, and
    `/health` (via the audio plane's `speaker_connected`) stops claiming a live
    speaker once the data plane is gone and the last sample is stale.
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
        self.adapter = Dot2Adapter("dot2", host="127.0.0.1", secrets_dir="secrets")
        self.adapter._link = self.link
        self.addCleanup(self.link.stop)
        self._sockets: list = []

    async def _register(self):
        ctl = await ws_client.connect(
            f"ws://127.0.0.1:{self.port}/control",
            additional_headers={"X-EM-Token": "secret"},
        )
        await ctl.send(
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
            await asyncio.wait_for(ctl.recv(), 2)
        self._sockets.append(ctl)
        return ctl

    async def _mic(self, seq: int = 1):
        data = await ws_client.connect(
            f"ws://127.0.0.1:{self.port}/data",
            additional_headers={"X-EM-Token": "secret"},
        )
        await asyncio.sleep(0.05)
        await data.send(bytes([FRAME_MIC]) + seq.to_bytes(2, "big") + bytes(320))
        self._sockets.append(data)
        return data

    async def _wait_until(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail("condition not reached in time")

    async def asyncTearDown(self) -> None:
        for ws in self._sockets:
            ws.transport.abort()

    async def test_a_power_cut_flips_health_and_a_reconnect_restores_it(self) -> None:
        with mock.patch.object(adapter_module, "SPEAKER_STALE_S", 0.4):
            ctl = await self._register()
            data = await self._mic()
            await self._wait_until(lambda: self.adapter.health()["connected"])
            self.assertTrue(self.adapter.health()["speaker_fresh"])

            # Power cut: both sockets vanish and nothing more arrives.
            ctl.transport.abort()
            data.transport.abort()
            await self._wait_until(lambda: not self.link.connected.is_set())
            health = self.adapter.health()
            self.assertFalse(health["connected"])
            self.assertFalse(health["data_plane"])

            # The device dials again (fresh register + fresh data plane).
            await self._register()
            await self._mic(seq=2)
            await self._wait_until(lambda: self.adapter.health()["connected"])
            self.assertTrue(self.adapter.health()["connected"])

    async def test_a_live_socket_with_a_quiet_data_plane_goes_stale(self) -> None:
        """A hung /data (socket open, no frames) must not stay green forever."""
        with mock.patch.object(adapter_module, "SPEAKER_STALE_S", 0.4):
            await self._register()
            await self._mic()
            await self._wait_until(lambda: self.adapter.health()["connected"])
            await asyncio.sleep(0.6)  # no mic frame, no playback write
            health = self.adapter.health()
            self.assertTrue(health["data_plane"], "the socket is still adopted")
            self.assertFalse(health["speaker_fresh"])
            self.assertFalse(health["connected"])


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

    async def test_drain_signals_does_not_carry_device_wakes(self) -> None:
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


PLAYBACK_ENV_PREFIX = "DOT2_PLAYBACK_"


def make_adapter_with_env(**env: object) -> Dot2Adapter:
    """Build an adapter with only the given DOT2_PLAYBACK_* variables present."""
    clean = {key: value for key, value in os.environ.items()
             if not key.startswith(PLAYBACK_ENV_PREFIX)}
    clean.update({key: str(value) for key, value in env.items()})
    with mock.patch.dict(os.environ, clean, clear=True):
        return make_adapter()


class PlaybackSilenceWindowTest(unittest.IsolatedAsyncioTestCase):
    """Plug 1.1.3 closes self-hearing at the source: the uplink is muted.

    The old two-window policy kept the mic live during playback (for barge) and
    for a 350 ms drain tail, which is exactly the audio rui's VAD hold buffers
    and replays as a fake request when the hold lifts. Barge now rides the
    device-wake channel (`barge_via_device`), so the mic stays muted for the
    whole playback and the 2500 ms that follow.
    """

    def _adapter(self, *, gate_s: float = 2.5, live_s: float = 0.0,
                 last: float | None = 100.0) -> Dot2Adapter:
        adapter = make_adapter()
        adapter._playback_gate_s = gate_s
        adapter._playback_live_s = live_s
        adapter._last_play_at = last
        return adapter

    def test_defaults_mute_playback_and_keep_the_2500ms_gate(self) -> None:
        adapter = make_adapter_with_env()
        self.assertEqual(adapter._playback_live_s, 0.0)
        self.assertEqual(adapter._playback_gate_s, 2.5)

    def test_muted_during_playback(self) -> None:
        adapter = self._adapter()
        for at in (100.001, 100.02, 100.2, 101.5):
            self.assertTrue(adapter._silence_window_active(at), at)

    def test_muted_until_the_2500ms_window_closes(self) -> None:
        adapter = self._adapter()
        self.assertTrue(adapter._silence_window_active(100.4))
        self.assertTrue(adapter._silence_window_active(101.5))
        self.assertTrue(adapter._silence_window_active(102.5))  # inclusive edge

    def test_open_again_after_the_window(self) -> None:
        adapter = self._adapter()
        self.assertFalse(adapter._silence_window_active(102.5001))

    async def test_play_arms_the_window(self) -> None:
        adapter = make_adapter()
        adapter._prebuffer_bytes = 0
        self.assertFalse(adapter._silence_window_active(time.monotonic()))
        await adapter.play(bytes(4800), sample_rate=48000, channels=1)
        self.assertIsNotNone(adapter._last_play_at)
        self.assertTrue(adapter._silence_window_active(adapter._last_play_at + 0.001))

    def test_live_ms_override_restores_the_old_two_window_policy(self) -> None:
        adapter = make_adapter_with_env(DOT2_PLAYBACK_LIVE_MS=350)
        self.assertEqual(adapter._playback_live_s, 0.35)
        adapter._last_play_at = 100.0
        self.assertFalse(adapter._silence_window_active(100.2))  # drain tail open
        self.assertTrue(adapter._silence_window_active(100.4))   # then muted

    def test_gate_zero_disables_the_mute(self) -> None:
        adapter = make_adapter_with_env(DOT2_PLAYBACK_GATE_MS=0)
        self.assertEqual(adapter._playback_gate_s, 0.0)
        adapter._last_play_at = 100.0
        self.assertFalse(adapter._silence_window_active(100.5))

    def test_never_played_means_open(self) -> None:
        self.assertFalse(self._adapter(last=None)._silence_window_active(100.5))

    async def test_stop_playback_opens_the_mic_for_a_barge(self) -> None:
        adapter = self._adapter()
        adapter._last_play_at = time.monotonic()
        await adapter.stop_playback()
        self.assertIsNone(adapter._last_play_at)
        self.assertFalse(adapter._silence_window_active(time.monotonic()))

    async def test_frames_are_zeroed_while_the_window_is_active(self) -> None:
        adapter = make_adapter()
        adapter._link.blocks = [b"\x10\x20" * 1280]  # non-silent 80 ms block
        adapter._last_play_at = time.monotonic()
        frames = await collect_frames(adapter)
        self.assertEqual(len(frames), 4)
        self.assertEqual(b"".join(frame.pcm for frame in frames), bytes(4 * 640))

    async def test_frames_pass_through_once_the_window_closed(self) -> None:
        block = b"\x10\x20" * 1280
        adapter = make_adapter()
        adapter._link.blocks = [block]
        adapter._last_play_at = time.monotonic() - 3.0
        frames = await collect_frames(adapter)
        self.assertEqual(b"".join(frame.pcm for frame in frames), block)

    async def test_device_wakes_still_barge_while_the_mic_is_muted(self) -> None:
        adapter = self._adapter()
        self.assertTrue(adapter._silence_window_active(100.5))
        adapter.inject_wake(ts_ms=1.0, barge=True)
        events = adapter.drain_wakes()
        self.assertEqual(len(events), 1)
        self.assertIs(events[0].barge, True)

    def test_gate_mic_keeps_the_reference_channels(self) -> None:
        adapter = self._adapter()
        adapter.ref_channels = 1
        adapter.channels = 2
        silenced = adapter._gate_mic(b"\x01\x02\x03\x04")
        self.assertEqual(silenced[:2], b"\x00\x00")
        self.assertEqual(silenced[2:], b"\x03\x04")


if __name__ == "__main__":
    unittest.main()
