"""Physical buttons on the Dot 2 (task-18).

The device sends evdev click codes, not key names: the identity of a physical
key is ``clickType`` and the mute state rides along as ``muted``. This module
owns the one mapping table and the signal object rui drains, so neither the
link nor the adapter has to grow a private copy of the numbers.

Mapping (from EchoMuse ``device/pkg/buttons/button.go``; evdev codes):

=======  ============  ============  ===================================
code     physical key  signal kind   text
=======  ============  ============  ===================================
113      mute          mute/unmute   ``muted`` / ``unmuted``
114      volume down   volume_down   observed ``muted`` state
115      volume up     volume_up     observed ``muted`` state
138      action (dot)  action        observed ``muted`` state
=======  ============  ============  ===================================

Only the press (``down: true``) is an event: the matching release would
otherwise double every volume step. Unknown codes are ignored and logged once
per boot by the adapter; a future firmware key must not fake an action.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: clickType -> the signal rui consumes. ``113`` is stateful (mute/unmute).
CLICK_TYPE_KINDS: dict[int, str] = {
    113: "mute",
    114: "volume_down",
    115: "volume_up",
    138: "action",
}

#: Human names for the README/log line; never used as the wire identity.
CLICK_TYPE_NAMES: dict[int, str] = {
    113: "mute",
    114: "volume-down",
    115: "volume-up",
    138: "action",
}


@dataclass(frozen=True)
class ButtonSignal:
    """One drained event. ``kind``/``text``/``ts`` are the contract drain shape."""

    kind: str
    text: str = ""
    ts: float = field(default_factory=time.monotonic)


def click_type(value: object) -> int | None:
    """The payload's clickType as an int, or None when it is absent/malformed."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def kind_for(click_type_value: object) -> str | None:
    """Mapped signal kind for one clickType, or None for an unknown key."""
    code = click_type(click_type_value)
    return CLICK_TYPE_KINDS.get(code) if code is not None else None


def mute_text(muted: object) -> str:
    """The observed mute state as the signal's diagnostic text."""
    if muted is True:
        return "muted"
    if muted is False:
        return "unmuted"
    return ""
