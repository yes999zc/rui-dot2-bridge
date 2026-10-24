"""Device volume: percent <-> the EchoMuse level scale, with a hard ceiling.

The device's level is the codec's own 0.5 dB/step law, 0 = -63.5 dB and
127 = unity gain (0 dB). Above 127 the DAC saturated, which is why the
firmware stops there.

The ceiling is configured per deployment (``DOT2_VOLUME_CAP``). It started at
30 % for the overnight runs, where 30 % turned out to be -44.5 dB and inaudible;
this deployment runs at 100 %. Every path goes through the cap: the value set at
connect, what a reply asks for, and what the physical buttons produce on the
device (the link pushes it back down).
"""

from __future__ import annotations

DEVICE_LEVEL_MAX = 127

#: Hard ceiling for this device, in percent of the device's own scale.
VOLUME_CAP_PERCENT = 30.0


def percent_to_level(percent: float) -> int:
    """Map 0-100 % onto the device's 0-127 scale."""
    return max(0, min(DEVICE_LEVEL_MAX, round(percent / 100.0 * DEVICE_LEVEL_MAX)))


def level_to_percent(level: int) -> float:
    return 100.0 * max(0, min(DEVICE_LEVEL_MAX, level)) / DEVICE_LEVEL_MAX


def clamp_percent(percent: float, cap: float = VOLUME_CAP_PERCENT) -> float:
    """Clamp a requested volume to the ceiling the owner set for this speaker."""
    if percent != percent:  # NaN guard: a bad request must not become silence or full
        return 0.0
    return max(0.0, min(float(percent), float(cap)))


def capped_level(percent: float, cap: float = VOLUME_CAP_PERCENT) -> int:
    """Percent in, device level out, ceiling applied."""
    return percent_to_level(clamp_percent(percent, cap))
