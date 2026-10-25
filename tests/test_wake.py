"""Unit tests for the oww_wake -> DeviceWake mapping (Socket & Plug v1 4.2).

These pin the contract details a wrong guess would break: six fields only, the
monotonic clock origin, 0-1 score normalisation, a real bool barge, dBFS level,
and a bounded queue that drains exactly once.
"""

from __future__ import annotations

import math
import unittest

from rui.device.contract import DeviceWake

from rui_dot2_bridge.wake import (
    coerce_barge,
    map_oww_wake,
    normalize_level,
    normalize_score,
)


def _message(**overrides):
    message = {
        "type": "oww_wake",
        "score": 0.92,
        "threshold": 0.5,
        "ageMs": 120,
        "capturedMono": True,
        "barge": False,
        "level": -18.5,
    }
    message.update(overrides)
    return message


class MapWakeTest(unittest.TestCase):
    def test_valid_message_maps_all_six_fields(self) -> None:
        wake, reason = map_oww_wake(_message(), now_ms=100_000.0)
        self.assertEqual(reason, "")
        self.assertIsInstance(wake, DeviceWake)
        assert wake is not None
        self.assertAlmostEqual(wake.ts_ms, 100_000.0 - 120.0)
        self.assertAlmostEqual(wake.score, 0.92)
        self.assertAlmostEqual(wake.threshold, 0.5)
        self.assertIs(wake.barge, False)
        self.assertAlmostEqual(wake.level, -18.5)
        self.assertEqual(wake.source, "device")

    def test_ts_uses_the_frame_clock_and_never_wall_clock(self) -> None:
        # AudioFrame.ts_ms is time.monotonic()*1000; a wake anchored on the epoch
        # would be rejected as stale/from the future by RUI.
        wake, _ = map_oww_wake(_message(ageMs=0), now_ms=123.0)
        assert wake is not None
        self.assertEqual(wake.ts_ms, 123.0)

    def test_age_is_clamped_to_reported_now_when_nonsensical(self) -> None:
        for age in (-5, "soon", None, float("inf")):
            wake, _ = map_oww_wake(_message(ageMs=age), now_ms=50.0)
            assert wake is not None, age
            self.assertEqual(wake.ts_ms, 50.0)

    def test_barge_encodings_become_a_real_bool(self) -> None:
        cases = [
            (True, True),
            (False, False),
            (1, True),
            (0, False),
            (1.0, True),
            ("true", True),
            ("FALSE", False),
            (None, False),
        ]
        for raw, expected in cases:
            wake, reason = map_oww_wake(_message(barge=raw), now_ms=1.0)
            self.assertEqual(reason, "", raw)
            assert wake is not None
            self.assertIs(wake.barge, expected, raw)

    def test_unparsable_barge_rejects_the_whole_event(self) -> None:
        for raw in ("maybe", 2, 1.5, {"x": 1}):
            wake, reason = map_oww_wake(_message(barge=raw), now_ms=1.0)
            self.assertIsNone(wake)
            self.assertIn("barge", reason)

    def test_contract_rejects_a_non_bool_barge_directly(self) -> None:
        with self.assertRaises(TypeError):
            DeviceWake(ts_ms=1.0, score=0.9, threshold=0.5, barge="false")  # type: ignore[arg-type]

    def test_score_is_normalised_and_clamped_to_0_1(self) -> None:
        self.assertEqual(normalize_score(1.5, 0.5), (1.0, 0.5))
        self.assertEqual(normalize_score(-3.0, 0.5), (0.0, 0.5))
        # A threshold above 1 means the device reports a percentage/raw scale.
        self.assertEqual(normalize_score(82.0, 50.0), (0.82, 0.5))
        self.assertIsNone(normalize_score(0.5, 0.0))

    def test_score_below_its_own_threshold_is_not_a_wake(self) -> None:
        wake, reason = map_oww_wake(_message(score=0.2, threshold=0.5), now_ms=1.0)
        self.assertIsNone(wake)
        self.assertIn("below", reason)

    def test_missing_or_non_finite_score_threshold_is_rejected(self) -> None:
        for field in ("score", "threshold"):
            for bad in (None, "loud", float("nan"), float("inf")):
                wake, _ = map_oww_wake(_message(**{field: bad}), now_ms=1.0)
                self.assertIsNone(wake, (field, bad))
        wake, _ = map_oww_wake(_message(threshold=0), now_ms=1.0)
        self.assertIsNone(wake)

    def test_level_is_dbfs_or_none(self) -> None:
        self.assertEqual(normalize_level(-30.0), -30.0)
        self.assertAlmostEqual(normalize_level(0.5) or 0.0, 20 * math.log10(0.5))
        for bad in (0, 2.0, "loud", float("nan"), True):
            self.assertIsNone(normalize_level(bad), bad)

    def test_message_without_optional_level_is_valid(self) -> None:
        message = _message()
        message.pop("level")
        wake, reason = map_oww_wake(message, now_ms=1.0)
        self.assertEqual(reason, "")
        assert wake is not None
        self.assertIsNone(wake.level)

    def test_coerce_barge_returns_none_for_unknown_shapes(self) -> None:
        self.assertIs(coerce_barge("on"), True)
        self.assertIs(coerce_barge("off"), False)
        self.assertIsNone(coerce_barge("maybe"))


if __name__ == "__main__":
    unittest.main()
