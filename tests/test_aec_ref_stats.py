"""AEC diagnostics use device reports, without exposing arbitrary stats fields."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from rui_dot2_bridge.device_link import DeviceLink

LOGGER = "rui_dot2_bridge.device_link"


class AecRefStatsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.link = DeviceLink(device_id="dot2", aec_enabled=True, aec_ref_source="sw")

    async def _stats(self, value: object) -> None:
        await self.link._on_control_message(json.dumps({
            "type": "stats", "aecRef": value, "token": "not-for-info-logs",
        }))

    async def test_only_changed_allowed_device_reports_are_logged(self) -> None:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            for value in ("off", "off", "hw", "hw", "sw", "sw"):
                await self._stats(value)
        self.assertEqual([record.getMessage() for record in captured.records], [
            f"dot2 link: device stats dot2 aecRef={value}"
            for value in ("off", "hw", "sw")
        ])
        self.assertEqual(self.link._last_reported_aec_ref, "sw")

    async def test_invalid_reports_do_not_log_or_replace_last_observation(self) -> None:
        with self.assertNoLogs(LOGGER, level="INFO"):
            for value in (None, True, 1, [], {}, "HW", "unknown", "sw\ntoken"):
                await self._stats(value)
            await self.link._on_control_message('{"type":"stats"}')
        self.assertIsNone(self.link._last_reported_aec_ref)
        with self.assertLogs(LOGGER, level="INFO"):
            await self._stats("hw")
        with self.assertNoLogs(LOGGER, level="INFO"):
            await self._stats({"aecRef": "sw"})
            await self._stats("hw")
        self.assertEqual(self.link._last_reported_aec_ref, "hw")

    async def test_new_control_connection_requires_a_fresh_report(self) -> None:
        first, second = mock.Mock(), mock.Mock()
        self.link._adopt_control(first)
        with self.assertLogs(LOGGER, level="INFO"):
            # The request is sw, but the observed device state is off.
            await self._stats("off")
        with self.assertLogs(LOGGER, level="INFO") as captured:
            self.link._adopt_control(second)
            self.assertIsNone(self.link._last_reported_aec_ref)
            await self._stats("off")
            await self._stats("off")
        reports = [record.getMessage() for record in captured.records
                   if "aecRef=" in record.getMessage()]
        self.assertEqual(reports, ["dot2 link: device stats dot2 aecRef=off"])
        first.transport.abort.assert_called_once()


if __name__ == "__main__":
    unittest.main()
