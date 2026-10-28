"""Physical buttons (task-18): clickType mapping, mute state, and drained signals.

The firmware sends the evdev click codes as the identity (``clickType``), the
physical key is not in the payload. These tests pin the four values the Dot 2
actually emits, the ``muted`` state that must survive the mapping, and the
"unknown is logged once, never acted on" rule.
"""

from __future__ import annotations

import json
import logging
import unittest

from rui_dot2_bridge.adapter import Dot2Adapter
from rui_dot2_bridge.device_link import DeviceLink


def make_adapter() -> tuple[Dot2Adapter, DeviceLink]:
    """An adapter wired to a real (non-listening) link, as ``open()`` would wire it."""
    adapter = Dot2Adapter("dot2", host="127.0.0.1", secrets_dir="secrets")
    link = DeviceLink(
        tls_port=None,
        plain_port=None,
        device_id="dot2",
        on_button=adapter._on_button,
    )
    adapter._link = link
    return adapter, link


async def press(link: DeviceLink, click_type: int, *, muted: bool, down: bool = True) -> None:
    await link._on_control_message(
        json.dumps(
            {
                "type": "button",
                "button": {"type": "Dot"},
                "clickType": click_type,
                "down": down,
                "heldMs": 0,
                "muted": muted,
            }
        )
    )


class ButtonSignalTest(unittest.IsolatedAsyncioTestCase):
    async def test_mute_frame_calls_on_button_and_updates_health(self) -> None:
        adapter, link = make_adapter()
        seen: list[dict] = []

        async def spy(message: dict) -> None:
            seen.append(message)
            await adapter._on_button(message)

        link.on_button = spy

        await press(link, 113, muted=True)

        self.assertEqual(len(seen), 1, "the injected on_button callback was not called")
        self.assertTrue(link.muted)
        self.assertTrue(adapter.health()["muted"])
        signals = adapter.drain_signals()
        self.assertEqual([signal.kind for signal in signals], ["mute"])
        self.assertEqual(signals[0].text, "muted")

    async def test_mute_toggle_false_reports_unmute(self) -> None:
        adapter, link = make_adapter()
        await press(link, 113, muted=True)
        await press(link, 113, muted=False)

        self.assertFalse(link.muted)
        self.assertFalse(adapter.health()["muted"])
        self.assertEqual(
            [signal.kind for signal in adapter.drain_signals()], ["mute", "unmute"]
        )

    async def test_volume_keys_emit_clear_events_with_observed_mute(self) -> None:
        adapter, link = make_adapter()
        await press(link, 115, muted=True)
        await press(link, 114, muted=False)

        signals = adapter.drain_signals()
        self.assertEqual([signal.kind for signal in signals], ["volume_up", "volume_down"])
        self.assertEqual([signal.text for signal in signals], ["muted", "unmuted"])

    async def test_action_button_maps_to_action(self) -> None:
        adapter, link = make_adapter()
        await press(link, 138, muted=False)
        signals = adapter.drain_signals()
        self.assertEqual([signal.kind for signal in signals], ["action"])

    async def test_unknown_click_type_is_ignored_and_logged_once(self) -> None:
        adapter, link = make_adapter()
        logger = "rui_dot2_bridge.adapter"
        with self.assertLogs(logger, level=logging.INFO) as captured:
            await press(link, 999, muted=False)
            await press(link, 999, muted=False)
        self.assertEqual(adapter.drain_signals(), [])
        self.assertEqual(
            sum("clickType 999" in line for line in captured.output),
            1,
            f"unknown clickType must be reported once, got {captured.output}",
        )

    async def test_press_is_one_event_not_two(self) -> None:
        """down=true is the action; the matching release must not double-fire."""
        adapter, link = make_adapter()
        await press(link, 115, muted=False, down=True)
        await press(link, 115, muted=False, down=False)
        self.assertEqual([signal.kind for signal in adapter.drain_signals()], ["volume_up"])

    def test_adapter_injects_its_button_handler(self) -> None:
        adapter = Dot2Adapter("dot2", host="127.0.0.1", secrets_dir="secrets")
        injected = adapter._link_kwargs["on_button"]
        self.assertIsNotNone(injected)
        self.assertIs(injected.__self__, adapter)
        self.assertIs(injected.__func__, Dot2Adapter._on_button)


if __name__ == "__main__":
    unittest.main()
