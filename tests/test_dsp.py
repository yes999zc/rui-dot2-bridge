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
    Band,
    BiquadStream,
    FirStream,
    Limiter,
    LoudnessConfig,
    LoudnessProcessor,
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

    def test_config_roundtrip(self) -> None:
        path = self.dir / "loudness.json"
        config = LoudnessConfig(mode="custom", trim_db=2.5,
                                bands=[{"type": "peaking", "freq": 800, "gain_db": 2, "q": 1.2}])
        LoudnessProcessor.dump_config(path, config)
        loaded = LoudnessProcessor.load_config(path)
        self.assertEqual(loaded.mode, "custom")
        self.assertEqual(loaded.trim_db, 2.5)
        self.assertEqual(loaded.bands[0]["freq"], 800)


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
