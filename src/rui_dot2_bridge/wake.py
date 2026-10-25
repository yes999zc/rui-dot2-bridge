"""Map EchoMuse oww_wake control messages to the rui v1 DeviceWake.

Socket & Plug v1 (docs/socket-plug-v1.md section 4.2) gives an adapter exactly
one way to report a wake that the *device* detected with *our* wake word: a
bounded, synchronous drain_wakes() queue of rui.device.contract.DeviceWake
events. EchoMuse reports that detection on /control as
{"type": "oww_wake", ...} with the model score, the device decision threshold,
the age of the capture and an advisory barge hint. This module owns the
translation and its validation so neither the device link nor the adapter
re-implements it.

Two rules drive the mapping:

* Normalise, never fabricate. ts_ms is anchored on the same monotonic clock as
  AudioFrame.ts_ms (time.monotonic() * 1000) minus the device ageMs;
  score/threshold are brought onto the contract 0-1 scale using the scale the
  device itself reports; barge becomes a real bool; missing or unparsable
  optional values stay None.
* Reject what cannot be trusted. A message with no finite score, no positive
  threshold, an unparsable barge hint, or a score below its own threshold is
  not a wake. The caller counts the rejection and RUI KWS is untouched.

The real 0.3.x contract classes are imported when rui is installed. The local
fallbacks with identical shape exist only for the standalone bench
("python -m rui_dot2_bridge.standalone"), which runs without rui; the adapter
itself always imports rui.device.contract directly.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

try:  # production: the 0.3.x contract is the single authority
    from rui.device.contract import (  # type: ignore[import-not-found]
        MAX_PENDING_WAKES,
        BoundedWakeQueue,
        DeviceWake,
    )
except ImportError:  # pragma: no cover - only the standalone bench runs without rui
    MAX_PENDING_WAKES = 32

    @dataclass(frozen=True)
    class DeviceWake:  # type: ignore[no-redef]
        """Standalone-bench stand-in; same fields and validation as rui."""

        ts_ms: float
        score: float
        threshold: float
        barge: bool = False
        level: float | None = None
        source: str = "device"

        def __post_init__(self) -> None:
            if not isinstance(self.barge, bool):
                raise TypeError("DeviceWake.barge must be a bool")
            if self.source != "device":
                raise ValueError("DeviceWake.source must be 'device'")

    class BoundedWakeQueue:  # type: ignore[no-redef]
        """Thread-safe drop-oldest queue; same semantics as rui implementation."""

        def __init__(self, maxlen: int = MAX_PENDING_WAKES) -> None:
            if maxlen < 1:
                raise ValueError("maxlen must be >= 1")
            self._items: deque[DeviceWake] = deque(maxlen=int(maxlen))
            self._lock = threading.Lock()
            self.dropped = 0

        def push(self, wake: DeviceWake) -> None:
            with self._lock:
                if len(self._items) == self._items.maxlen:
                    self.dropped += 1
                self._items.append(wake)

        def drain(self) -> list[DeviceWake]:
            with self._lock:
                pending = list(self._items)
                self._items.clear()
                return pending

        def clear(self) -> None:
            with self._lock:
                self._items.clear()

        def __len__(self) -> int:
            with self._lock:
                return len(self._items)


__all__ = [
    "MAX_PENDING_WAKES",
    "BoundedWakeQueue",
    "DeviceWake",
    "coerce_barge",
    "map_oww_wake",
    "normalize_level",
    "normalize_score",
]

_TRUE_TEXT = frozenset({"true", "1", "yes", "on"})
_FALSE_TEXT = frozenset({"false", "0", "no", "off"})
#: Values at or below 0 dBFS and above this floor are accepted verbatim as dBFS.
_MIN_DBFS = -160.0


def _finite(value: object) -> float | None:
    """Return value as a finite float, or None (bools are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def coerce_barge(value: object) -> bool | None:
    """Turn the device advisory barge hint into a real bool.

    The contract rejects non-bool barge values because "false" and 1 are both
    truthy; an adapter therefore has to normalise the device JSON encoding
    before constructing DeviceWake. None/absent means the device sent no hint
    (False). Anything else is unparsable and the whole message is rejected
    rather than silently treated as False.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, float) and math.isfinite(value) and value in (0.0, 1.0):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TEXT:
            return True
        if text in _FALSE_TEXT:
            return False
    return None


def normalize_score(score: float, threshold: float) -> tuple[float, float] | None:
    """Map the device (score, threshold) pair onto the contract 0-1 scale.

    The device threshold is its own decision gate, so it shares the score
    scale. A threshold of at most 1 means the pair is already normalised (a
    score outside 0-1 is a model outlier and is clamped); a threshold above 1
    means a percentage/raw scale, which is divided down. When the scale is
    unknown (threshold above 100), the threshold itself is used as the unit so
    the pair keeps its decision boundary. Returns None for an unusable pair.
    """
    if not math.isfinite(threshold) or threshold <= 0.0:
        return None
    if threshold <= 1.0:
        return _clamp01(score), threshold
    scale = 100.0 if threshold <= 100.0 else threshold
    return _clamp01(score / scale), _clamp01(threshold / scale)


def normalize_level(value: object) -> float | None:
    """Return the optional device level as dBFS, or None when not convertible.

    0 is ambiguous (linear silence vs. 0 dBFS full scale), so it is reported
    as None instead of guessing. Values in (0, 1] are treated as a linear
    amplitude and converted with 20*log10.
    """
    number = _finite(value)
    if number is None:
        return None
    if _MIN_DBFS <= number < 0.0:
        return number
    if 0.0 < number <= 1.0:
        return 20.0 * math.log10(number)
    return None


def _age_ms(value: object) -> float:
    """Capture age in ms; an absent or nonsensical age means reported now."""
    number = _finite(value)
    if number is None or number < 0.0:
        return 0.0
    return number


def map_oww_wake(message: Mapping[str, Any], *, now_ms: float) -> tuple[DeviceWake | None, str]:
    """Translate one oww_wake control message.

    Returns (wake, "") for a valid event and (None, reason) otherwise. now_ms
    is the caller time.monotonic() * 1000; it is explicit so the mapping is
    deterministic in tests. capturedMono is accepted but not forwarded: it is a
    device diagnostic, and v1 deliberately has no field for it (no device_id,
    room or playing smuggling either).
    """
    if not isinstance(message, Mapping):
        return None, "message is not a mapping"

    score_raw = _finite(message.get("score"))
    if score_raw is None:
        return None, "score is missing or not a finite number"
    threshold_raw = _finite(message.get("threshold"))
    if threshold_raw is None or threshold_raw <= 0.0:
        return None, "threshold is missing, not finite, or not > 0"
    scaled = normalize_score(score_raw, threshold_raw)
    if scaled is None:  # pragma: no cover - guarded by the checks above
        return None, "score/threshold scale is unusable"
    score, threshold = scaled
    if score < threshold:
        return None, f"score {score:.3f} is below the device threshold {threshold:.3f}"

    barge = coerce_barge(message.get("barge"))
    if barge is None:
        return None, f"barge is not a bool (got {message.get('barge')!r})"

    level = normalize_level(message.get("level"))
    age = _age_ms(message.get("ageMs", message.get("age_ms")))
    captured_mono = message.get("capturedMono", message.get("captured_mono"))
    log.debug(
        "oww_wake: score=%.3f threshold=%.3f age=%.0f ms barge=%s level=%s captured_mono=%s",
        score,
        threshold,
        age,
        barge,
        level,
        captured_mono,
    )

    wake = DeviceWake(
        ts_ms=float(now_ms) - age,
        score=score,
        threshold=threshold,
        barge=barge,
        level=level,
    )
    return wake, ""
