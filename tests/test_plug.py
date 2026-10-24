"""Tests for the plug manifest and the defaults build() takes from it."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rui_dot2_bridge import plug
from rui_dot2_bridge.adapter import build

ROOT = Path(__file__).resolve().parents[1]


class ManifestTest(unittest.TestCase):
    def test_repo_manifest_parses_and_names_the_entrypoint(self) -> None:
        data = plug.load(ROOT / "plug.json")
        self.assertEqual(data["plug"], "dot2-echomuse")
        self.assertEqual(data["entrypoint"], "rui_dot2_bridge.adapter:build")
        self.assertEqual(data["audio"]["playback"]["rate"], 48000)
        self.assertEqual(data["audio"]["capture"]["rate"], 16000)
        self.assertEqual(data["volume"]["unity_at"], 127)

    def test_get_walks_nested_keys_and_defaults(self) -> None:
        data = plug.load(ROOT / "plug.json")
        self.assertEqual(plug.get(data, "audio", "playback", "prebuffer_ms"), 450)
        self.assertEqual(plug.get(data, "audio", "playback", "nope", default=7), 7)
        self.assertIsNone(plug.get(data, "missing"))

    def test_broken_or_missing_manifest_is_empty_not_fatal(self) -> None:
        with self.assertLogs("rui_dot2_bridge.plug", level="WARNING"):
            self.assertEqual(plug.load(Path("/nonexistent/plug.json")), {})
        broken = Path(self.id().replace(":", "_") + ".json")
        try:
            broken.write_text("{not json")
            with self.assertLogs("rui_dot2_bridge.plug", level="WARNING"):
                self.assertEqual(plug.load(broken), {})
        finally:
            broken.unlink(missing_ok=True)


class BuildDefaultsTest(unittest.TestCase):
    def device(self) -> SimpleNamespace:
        return SimpleNamespace(id="dot2", host="127.0.0.1", room="", secrets_dir="secrets",
                               listen_port=None, token_env=None)

    def test_build_inherits_manifest_defaults(self) -> None:
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("DOT2_")}
        with patch.dict(os.environ, clean, clear=True):
            adapter = build(self.device(), None)
        self.assertEqual(adapter.volume_cap_percent, 100.0)
        self.assertEqual(adapter._prebuffer_ms, 450)
        self.assertEqual(adapter._eos_idle_s, 1.2)
        self.assertEqual(adapter._mic_gain_db, 18)

    def test_env_overrides_the_manifest(self) -> None:
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("DOT2_")}
        clean.update({"DOT2_VOLUME_CAP": "55", "DOT2_PREBUFFER_MS": "300"})
        with patch.dict(os.environ, clean, clear=True):
            adapter = build(self.device(), None)
        self.assertEqual(adapter.volume_cap_percent, 55.0)
        self.assertEqual(adapter._prebuffer_ms, 300)


if __name__ == "__main__":
    unittest.main()
