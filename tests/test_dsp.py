"""Tests for the loudness stage: streaming FIR, limiter, runtime parameters, control API."""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
import unittest
import urllib.request
from pathlib import Path

import numpy as np

from rui_dot2_bridge.control import start_control_server
from rui_dot2_bridge.dsp import (
    CURVES,
    LADDER_BASS_TARGET_DB,
    LADDER_PRESENCE_FLOOR_DB,
    Band,
    BiquadStream,
    FirStream,
    Limiter,
    LoudnessConfig,
    LoudnessProcessor,
    build_ladder_curve,
)

RATE = 48000


def tone(seconds: float = 0.2, freq: float = 440.0, amp: float = 0.3) -> np.ndarray:
    n = int(RATE * seconds)
    return amp * np.sin(2 * math.pi * freq * np.arange(n) / RATE)


def pcm(x: np.ndarray) -> bytes:
    return np.clip(x, -1, 1).__mul__(32767).astype("<i2").tobytes()


class FirTest(unittest.TestCase):
    def test_streaming_matches_one_shot_convolution(self) -> None:
        rng = np.random.default_rng(7)
        taps = rng.standard_normal(64) / 64
        x = rng.standard_normal(1000)
        stream = FirStream(taps)
        pieces = [stream.process(x[i:i + 137]) for i in range(0, len(x), 137)]
        streamed = np.concatenate(pieces)
        direct = np.convolve(x, taps)[: len(x)]
        # overlap-add reproduces the same output once the initial tail has filled.
        np.testing.assert_allclose(streamed[200:], direct[200:], rtol=0, atol=1e-9)

    def test_impulse_response_is_the_taps(self) -> None:
        taps = np.array([0.5, 0.25, 0.125])
        stream = FirStream(taps)
        out = stream.process(np.array([1.0, 0.0, 0.0, 0.0]))
        np.testing.assert_allclose(out, [0.5, 0.25, 0.125, 0.0])


class LimiterTest(unittest.TestCase):
    def test_ceiling_is_respected(self) -> None:
        limiter = Limiter(RATE, ceiling_db=-1.0)
        x = tone(0.5, amp=0.9)
        out = limiter.process(np.concatenate([x, x]))
        self.assertLessEqual(float(np.max(np.abs(out))), 10 ** (-1.0 / 20.0))

    def test_quiet_signal_passes_through_delayed_by_lookahead(self) -> None:
        limiter = Limiter(RATE)
        x = tone(0.2, amp=0.05)
        out = limiter.process(x)
        delay = limiter.lookahead
        np.testing.assert_allclose(out[delay:], x[: len(x) - delay], atol=1e-9)

    def test_asymmetric_negative_peaks_are_limited(self) -> None:
        # Speech after the EQ can swing much further negative than positive; the
        # first implementation took the window max without abs() and let the
        # negative peaks hit the hard clip (audible "破音" on 2026-10-24).
        limiter = Limiter(RATE, ceiling_db=-1.0)
        x = np.full(4800, 0.2)
        x[::97] = -1.4
        x[48::97] = 0.5
        out = limiter.process(x)
        self.assertLessEqual(float(np.max(np.abs(out))), 10 ** (-1.0 / 20.0))

    def test_chunk_boundaries_do_not_leak_peaks(self) -> None:
        limiter = Limiter(RATE, ceiling_db=-1.0)
        x = tone(0.5, amp=0.9)
        parts = [limiter.process(x[i:i + 100]) for i in range(0, len(x), 100)]
        out = np.concatenate(parts)
        self.assertLessEqual(float(np.max(np.abs(out))), 10 ** (-1.0 / 20.0))


class BiquadTest(unittest.TestCase):
    def test_zero_gain_is_transparent(self) -> None:
        biquad = BiquadStream(Band(type="peaking", freq=1000, gain_db=0.0, q=0.7), RATE)
        x = tone(0.1)
        np.testing.assert_allclose(biquad.process(x), x, atol=1e-9)

    def test_positive_peak_raises_that_region(self) -> None:
        biquad = BiquadStream(Band(type="peaking", freq=1000, gain_db=6.0, q=1.0), RATE)
        x = tone(0.2, freq=1000)
        out = biquad.process(np.concatenate([np.zeros(4800), x]))[4800:]
        self.assertGreater(float(np.sqrt(np.mean(out ** 2))), float(np.sqrt(np.mean(x ** 2))) * 1.7)


class ProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # one unity curve so the stock mode is a transparent path
        (self.dir / "EQ_100.cfg").write_text("1.0, 0.0")
        for name in ("EQ_50", "EQ_60", "EQ_70", "EQ_80", "EQ_90"):
            (self.dir / f"{name}.cfg").write_text("1.0, 0.0")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def processor(self, **config) -> LoudnessProcessor:
        # tone shelves off by default in tests: they are evaluated separately
        config.setdefault("lowcut_db", 0.0)
        config.setdefault("highshelf_db", 0.0)
        # These tests exercise the raw FIR/limiter path with a unity curve; the
        # corrected ladder (which replaces the curve by design) has its own
        # fixture below.
        config.setdefault("ladder_correct", False)
        return LoudnessProcessor(LoudnessConfig(**config), self.dir)

    def test_stock_unity_curve_is_transparent_except_limiter(self) -> None:
        proc = self.processor(trim_db=0.0)
        x = tone(0.1, amp=0.1)
        out = np.frombuffer(proc.process_bytes(pcm(x)), dtype="<i2") / 32768.0
        delay = proc._limiter.lookahead
        np.testing.assert_allclose(out[delay:], x[: len(x) - delay], atol=1e-3)

    def test_trim_changes_level(self) -> None:
        proc = self.processor(trim_db=6.0)
        x = tone(0.1, amp=0.05, freq=1000.0)
        out = np.frombuffer(proc.process_bytes(pcm(x)), dtype="<i2") / 32768.0
        delay = proc._limiter.lookahead
        out = out[delay:]
        ref = x[: len(out)]
        ratio = float(np.sqrt(np.mean(out ** 2)) / np.sqrt(np.mean(ref ** 2)))
        self.assertAlmostEqual(20 * math.log10(ratio), 6.0, delta=0.3)

    def test_curve_ladder_follows_level(self) -> None:
        (self.dir / "EQ_50.cfg").write_text("2.0, 0.0")
        proc = self.processor(mode="stock", trim_db=0.0)
        proc.set_level(127)
        self.assertEqual(proc.curve_name, "EQ_100")
        proc.set_level(50)  # 50/127 ~ 39 % -> EQ_50
        self.assertEqual(proc.curve_name, "EQ_50")

    def test_config_update_is_live(self) -> None:
        proc = self.processor()
        proc.update({"mode": "custom", "trim_db": 3.0,
                     "bands": [{"type": "highshelf", "freq": 3000, "gain_db": 3, "q": 0.7}]})
        self.assertEqual(proc.config.mode, "custom")
        self.assertEqual(len(proc.config.bands), 1)
        out = proc.process_bytes(pcm(tone(0.05, amp=0.1)))
        self.assertGreater(len(out), 0)

    def test_describe_does_not_report_a_stale_curve(self) -> None:
        # GET /loudness used to keep saying EQ_100 (and its ladder tier) after
        # switching to custom/off, even though no stock FIR was playing.
        proc = self.processor(ladder_correct=True)
        self.assertEqual(proc.describe()["curve"], "EQ_100")
        self.assertIsNotNone(proc.describe()["ladder"]["tier"])

        proc.update({"mode": "custom", "bands": []})
        described = proc.describe()
        self.assertEqual(described["curve"], "")
        self.assertIsNone(described["ladder"]["tier"])
        self.assertEqual(described["ladder"], {"corrected": True, "tier": None})

        proc.update({"mode": "off"})
        self.assertEqual(proc.describe()["curve"], "")

        proc.update({"mode": "stock"})
        self.assertEqual(proc.describe()["curve"], "EQ_100")

    def test_tone_shelves_shape_the_bands(self) -> None:
        proc = self.processor(lowcut_db=6.0, highshelf_db=2.0)
        low = tone(0.2, freq=150.0, amp=0.1)
        high = tone(0.2, freq=8000.0, amp=0.05)
        low_out = np.frombuffer(proc.process_bytes(pcm(low)), dtype="<i2") / 32768.0
        high_out = np.frombuffer(proc.process_bytes(pcm(high)), dtype="<i2") / 32768.0
        low_gain = 20 * math.log10(max(np.sqrt(np.mean(low_out ** 2)), 1e-9) / np.sqrt(np.mean(low ** 2)))
        high_gain = 20 * math.log10(max(np.sqrt(np.mean(high_out ** 2)), 1e-9) / np.sqrt(np.mean(high ** 2)))
        self.assertLess(low_gain, -3.0)
        self.assertLess(high_gain, -1.0)

    def test_missing_curves_fall_back_to_flat(self) -> None:
        proc = LoudnessProcessor(LoudnessConfig(mode="stock", lowcut_db=0.0, highshelf_db=0.0, trim_db=0.0),
                                 Path(self.dir) / "nope")
        x = tone(0.05, amp=0.1)
        out = np.frombuffer(proc.process_bytes(pcm(x)), dtype="<i2") / 32768.0
        delay = proc._limiter.lookahead
        np.testing.assert_allclose(out[delay:], x[: len(x) - delay], atol=1e-3)

    def test_reset_stream_drops_the_previous_tail(self) -> None:
        proc = self.processor()
        first = proc.process_bytes(pcm(tone(0.05, amp=0.5)))
        self.assertGreater(len(first), 0)
        proc.reset_stream()
        silence = proc.process_bytes(pcm(np.zeros(4800)))
        out = np.frombuffer(silence, dtype="<i2") / 32768.0
        self.assertLess(float(np.max(np.abs(out))), 1e-6)

    def test_config_roundtrip(self) -> None:
        path = self.dir / "loudness.json"
        config = LoudnessConfig(mode="custom", trim_db=2.5,
                                bands=[{"type": "peaking", "freq": 800, "gain_db": 2, "q": 1.2}])
        LoudnessProcessor.dump_config(path, config)
        loaded = LoudnessProcessor.load_config(path)
        self.assertEqual(loaded.mode, "custom")
        self.assertEqual(loaded.trim_db, 2.5)
        self.assertEqual(loaded.bands[0]["freq"], 800)


def response_db(taps: np.ndarray, freqs: list[float]) -> np.ndarray:
    """Magnitude response of a FIR at ``freqs``, in dB (not normalised)."""
    n = 1 << 16
    magnitude = np.abs(np.fft.rfft(np.asarray(taps, dtype=float), n))
    grid = np.fft.rfftfreq(n, 1.0 / RATE)
    return 20.0 * np.log10(np.maximum(np.interp(freqs, grid, magnitude), 1e-12))


def band_mean_db(db: np.ndarray, freqs: list[float], low: float, high: float) -> float:
    return float(np.mean([value for freq, value in zip(freqs, db, strict=False) if low <= freq <= high]))


def pink_kick_music(seconds: float = 10.0) -> np.ndarray:
    """Deterministic stand-in for the diagnostic sample: pink noise + 60 Hz kick.

    The 2026-10-26 diagnosis measured the old chain on this programme; keeping
    the generator here means the limiter regression can be reproduced without
    the (gitignored) production recording.
    """
    rng = np.random.default_rng(3)
    n = int(RATE * seconds)
    spectrum = rng.standard_normal(n // 2 + 1) + 1j * rng.standard_normal(n // 2 + 1)
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    freqs[0] = freqs[1]
    spectrum /= np.sqrt(freqs)
    signal = np.fft.irfft(spectrum, n)
    signal /= math.sqrt(float(np.mean(signal ** 2)))
    kick_t = np.arange(int(0.12 * RATE)) / RATE
    kick = np.sin(2 * math.pi * 60.0 * kick_t) * np.exp(-kick_t / 0.04)
    for start in range(0, n - len(kick), int(0.5 * RATE)):
        signal[start:start + len(kick)] += 0.9 * kick
    spectrum = np.fft.rfft(signal)
    spectrum[freqs > 7900] = 0.0
    signal = np.fft.irfft(spectrum, n)
    return signal / math.sqrt(float(np.mean(signal ** 2))) * 10 ** (-15.4 / 20.0)


class CorrectedLadderTest(unittest.TestCase):
    """The six tiers must be six curves, and the bass must not swamp the mids.

    The vendor `EQ_50/60/70.cfg` files are byte-identical and every vendor curve
    has the same +20…+26 dB bass hump; that is what produced "+23 dB 100-300 Hz
    over 1-3 kHz" on the device. The corrected ladder is designed from the tier
    index, so it stays distinct even when the pulled files are not.
    """

    #: Device levels that select EQ_50 … EQ_100 through curve_for_level().
    TIER_LEVELS = (50, 70, 80, 95, 108, 120)
    FREQS = [20, 30, 47, 50, 60, 70, 80, 100, 125, 150, 174, 200, 250, 300,
             400, 500, 700, 1000, 1500, 2000, 3000, 4000, 5000, 6000, 7000, 8000]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # Deliberately degenerate: EQ_50/60/70 are the same file. The corrected
        # ladder must still produce three distinct tiers.
        for name in CURVES:
            (self.dir / f"{name}.cfg").write_text("1.0, 0.0")

    def processor(self, **config) -> LoudnessProcessor:
        config.setdefault("lowcut_db", 0.0)
        config.setdefault("highshelf_db", 0.0)
        config.setdefault("trim_db", 0.0)
        config.setdefault("ladder_correct", True)
        return LoudnessProcessor(LoudnessConfig(**config), self.dir)

    def measure(self, proc: LoudnessProcessor) -> tuple[float, float, dict[str, float]]:
        db = response_db(proc._fir.taps, self.FREQS)
        ref = float(np.interp(1000.0, self.FREQS, db))
        bass = band_mean_db(db, self.FREQS, 100, 300) - band_mean_db(db, self.FREQS, 1000, 3000)
        presence = band_mean_db(db, self.FREQS, 3000, 6000) - band_mean_db(db, self.FREQS, 1000, 3000)
        points = {str(freq): float(value - ref) for freq, value in zip(self.FREQS, db, strict=False)
                  if freq in (100, 200, 500, 1000, 2000, 4000, 8000)}
        return bass, presence, points

    def test_six_tiers_are_distinct_curves(self) -> None:
        proc = self.processor()
        curves: dict[str, np.ndarray] = {}
        for level in self.TIER_LEVELS:
            name = proc.set_level(level)
            curves[name] = proc._fir.taps.copy()
            np.testing.assert_allclose(curves[name], build_ladder_curve(name, proc.config, RATE),
                                       atol=1e-9, err_msg=f"{name} not wired into the FIR")
        self.assertEqual(tuple(curves), CURVES)

        responses = {name: response_db(taps, self.FREQS) for name, taps in curves.items()}
        names = list(responses)
        for i, left in enumerate(names):
            for right in names[i + 1:]:
                spread = float(np.max(np.abs(responses[left] - responses[right])))
                self.assertGreater(spread, 0.5, f"{left}/{right} differ by only {spread:.2f} dB")

        bass = [self.measure_at(responses[name])[0] for name in names]
        presence = [self.measure_at(responses[name])[1] for name in names]
        for index in range(len(names) - 1):
            self.assertGreater(bass[index], bass[index + 1] + 0.2,
                               f"bass not monotonic at {names[index]}→{names[index + 1]}: {bass}")
            self.assertLess(presence[index], presence[index + 1] - 0.2,
                            f"presence not monotonic at {names[index]}→{names[index + 1]}: {presence}")
        # The audible anchors the owner complained about: the 100 Hz *boost*
        # (relative to 1 kHz) falls with tier, the 4 kHz lift rises.
        relative = {name: db - float(np.interp(1000.0, self.FREQS, db))
                    for name, db in responses.items()}
        low = [float(np.interp(100.0, self.FREQS, relative[name])) for name in names]
        high = [float(np.interp(4000.0, self.FREQS, relative[name])) for name in names]
        self.assertEqual(low, sorted(low, reverse=True), f"100 Hz not falling with tier: {low}")
        self.assertEqual(high, sorted(high), f"4 kHz not rising with tier: {high}")

    def measure_at(self, db: np.ndarray) -> tuple[float, float]:
        bass = band_mean_db(db, self.FREQS, 100, 300) - band_mean_db(db, self.FREQS, 1000, 3000)
        presence = band_mean_db(db, self.FREQS, 3000, 6000) - band_mean_db(db, self.FREQS, 1000, 3000)
        return bass, presence

    def test_bass_overshoot_stays_within_the_target(self) -> None:
        proc = self.processor()
        for level in self.TIER_LEVELS:
            name = proc.set_level(level)
            bass, presence, points = self.measure(proc)
            self.assertLessEqual(bass, LADDER_BASS_TARGET_DB,
                                 f"{name}: 100-300 Hz is {bass:+.1f} dB over 1-3 kHz")
            self.assertGreaterEqual(bass, 0.0, f"{name}: corrected ladder lost all warmth")
            self.assertGreaterEqual(presence, LADDER_PRESENCE_FLOOR_DB,
                                    f"{name}: 3-6 kHz is {presence:+.1f} dB under 1-3 kHz")
            # Key points relative to 1 kHz (same list the diagnosis used).
            self.assertLessEqual(points["100"], LADDER_BASS_TARGET_DB + 1.0)
            self.assertGreaterEqual(points["4000"], -0.5, f"{name}: presence hole at 4 kHz")

    def test_sub_bass_rolloff_is_realised_at_1024_taps(self) -> None:
        """20-47 Hz must follow the 2nd-order target, not just the top tier.

        The former 1024-point design could not describe anything below its first
        bin (46.9 Hz): ``freqs[0]=freqs[1]`` left EQ_100 at -2.6 dB @ 20 Hz, i.e.
        no roll-off, and the kick drum still drove the limiter. The target is now
        designed on an 8192-point grid and truncated to 1024 taps.
        """
        proc = self.processor()
        self.assertEqual(proc.set_level(120), "EQ_100")
        db = response_db(proc._fir.taps, self.FREQS)
        ref = float(np.interp(1000.0, self.FREQS, db))
        points = {freq: float(np.interp(freq, self.FREQS, db)) - ref
                  for freq in (20, 30, 47, 60, 100)}

        # Design target: -17 dB at 20 Hz for EQ_100.
        self.assertAlmostEqual(points[20], -17.0, delta=2.0,
                               msg=f"EQ_100 20 Hz is {points[20]:+.1f} dB (want -17 ±2)")
        # A 2nd-order roll-off rises monotonically from 20 to 60 Hz, and 47 Hz is
        # audibly below the 100 Hz bass plateau (>4 dB), not just a fraction.
        self.assertLess(points[20], points[30] - 3.0, f"no slope 20-30 Hz: {points}")
        self.assertLess(points[30], points[47] - 2.0, f"no slope 30-47 Hz: {points}")
        self.assertLess(points[47], points[60] - 1.0, f"no slope 47-60 Hz: {points}")
        self.assertLessEqual(points[47], -2.0, f"47 Hz not rolled off: {points[47]:+.1f} dB")
        self.assertLess(points[47], points[100] - 4.0,
                        f"47 Hz only {points[47] - points[100]:+.1f} dB under the bass plateau")

        # Truncating the 8192-point design to 1024 taps keeps the stock group
        # delay: 512 samples @ 48 kHz = 10.67 ms.
        peak = int(np.argmax(np.abs(proc._fir.taps)))
        self.assertEqual(peak, 512, "linear-phase peak moved off the stock delay")
        self.assertAlmostEqual(peak / RATE * 1000.0, 10.67, delta=0.01)
        self.assertEqual(len(proc._fir.taps), 1024)

    def test_limiter_does_not_crush_and_never_clips(self) -> None:
        proc = self.processor()
        self.assertEqual(proc.set_level(120), "EQ_100")
        proc.reset_stream()
        signal = pink_kick_music()
        raw = np.clip(signal, -1.0, 1.0).__mul__(32767).astype("<i2").tobytes()
        out = np.frombuffer(proc.process_bytes(raw), dtype="<i2") / 32768.0

        peak = float(np.max(np.abs(out)))
        self.assertLessEqual(peak, 10 ** (-0.9 / 20.0), f"output peak {20 * math.log10(peak):+.2f} dBFS")
        self.assertLess(peak, 0.95, "limiter left samples near int16 full scale")
        # The old chain averaged -12.9 dB of gain reduction on this programme;
        # the corrected ladder measures about -1.3 dB (the sub-bass roll-off
        # stopped the kick from moving the whole phrase). A floor of -6 dB alone
        # was useless: setting average_gain_db to a constant 0 passed it. The
        # upper bound below is the real regression guard.
        average = proc._limiter.average_gain_db
        self.assertGreaterEqual(average, -6.0, f"limiter works too hard: {average:+.1f} dB")
        self.assertLessEqual(average, -1.0,
                             f"limiter is not acting on this loud programme: {average:+.2f} dB")

    def test_silence_and_low_level_do_not_trigger_limiting(self) -> None:
        # A constant-zero average_gain_db implementation would also pass the
        # loud-programme bound above, so pin the other side: quiet input must
        # report ~0 dB of gain reduction.
        proc = self.processor()
        self.assertEqual(proc.set_level(120), "EQ_100")
        proc.reset_stream()
        proc.process_bytes(pcm(np.zeros(RATE // 10)))
        self.assertAlmostEqual(proc._limiter.average_gain_db, 0.0, delta=0.01)

        proc.reset_stream()
        proc.process_bytes(pcm(tone(0.5, freq=440.0, amp=0.02)))
        self.assertAlmostEqual(proc._limiter.average_gain_db, 0.0, delta=0.01)

    def test_reset_stream_clears_limiter_statistics(self) -> None:
        proc = self.processor()
        self.assertEqual(proc.set_level(120), "EQ_100")
        proc.reset_stream()
        proc.process_bytes(pcm(pink_kick_music(2.0)))
        self.assertLessEqual(proc._limiter.average_gain_db, -0.5)
        self.assertGreater(proc._limiter.gain_db_count, 0)

        proc.reset_stream()
        self.assertEqual(proc._limiter.average_gain_db, 0.0)
        self.assertEqual(proc._limiter.gain_db_count, 0)
        self.assertEqual(proc._limiter.min_gain, 1.0)
        self.assertEqual(proc._limiter.max_gain, 1.0)

    def test_legacy_mode_still_replays_the_vendor_curve(self) -> None:
        proc = self.processor(ladder_correct=False)
        proc.set_level(120)
        expected = np.asarray([float(part) for part in
                               (self.dir / "EQ_100.cfg").read_text().replace("\n", " ").split(",")
                               if part.strip()])
        np.testing.assert_allclose(proc._fir.taps, expected)


class ControlTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "EQ_100.cfg").write_text("1.0, 0.0")
        self.processor = LoudnessProcessor(LoudnessConfig(mode="stock"), self.dir)
        self.persist = self.dir / "loudness.json"
        self.server = await start_control_server(self.processor, "127.0.0.1", 0, self.persist)
        self.port = self.server.sockets[0].getsockname()[1]
        self.addCleanup(self._close)

    async def _close(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.loads(response.read())

    async def test_get_and_put(self) -> None:
        current = await asyncio.to_thread(self.request, "GET", "/loudness")
        self.assertTrue(current["enabled"])
        updated = await asyncio.to_thread(
            self.request, "PUT", "/loudness", {"enabled": False, "trim_db": 1.5})
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["trim_db"], 1.5)
        persisted = json.loads(self.persist.read_text())
        self.assertFalse(persisted["enabled"])

    async def test_health(self) -> None:
        health = await asyncio.to_thread(self.request, "GET", "/health")
        self.assertTrue(health["ok"])


if __name__ == "__main__":
    unittest.main()
