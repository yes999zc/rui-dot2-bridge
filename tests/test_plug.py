"""Tests for the plug manifest and the defaults build() takes from it."""

from __future__ import annotations

import os
import tempfile
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


class BuildParamsTest(unittest.TestCase):
    """DeviceConfig (and its params) outranks DOT2_* env, which outranks nothing."""

    def device(self, **overrides) -> SimpleNamespace:
        values = {
            "id": "dot2",
            "host": "devhost",
            "room": "",
            "secrets_dir": "secrets",
            "listen_port": None,
            "token_env": None,
            "params": {},
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def env(self, extra: dict | None = None):
        clean = {key: value for key, value in os.environ.items() if not key.startswith("DOT2_")}
        if extra:
            clean.update(extra)
        return patch.dict(os.environ, clean, clear=True)

    def test_device_params_win_over_env(self) -> None:
        device = self.device(
            host="",
            token_env="DOT2_EM_TOKEN",
            params={
                "host": "paramhost",
                "room": "书房",
                "port": 9001,
                "plug_id": "dot2-echomuse",
                "oww_model": "hey_rui.onnx",
            },
        )
        with self.env(
            {
                "DOT2_HOST": "envhost",
                "DOT2_ROOM": "envroom",
                "DOT2_LISTEN_PORT": "9002",
                "DOT2_EM_TOKEN": "tok",
            }
        ):
            adapter = build(device, None)
        self.assertEqual(adapter.host, "paramhost")
        self.assertEqual(adapter.room, "书房")
        self.assertEqual(adapter._link_kwargs["tls_port"], 9001)
        self.assertEqual(adapter.plug_id, "dot2-echomuse")
        self.assertEqual(adapter.oww_model, "hey_rui.onnx")
        self.assertEqual(adapter.oww_on_device, "on")
        self.assertEqual(adapter._link_kwargs["token"], "tok")
        self.assertIsNotNone(adapter._link_kwargs["wake_queue"])

    def test_device_config_wins_over_params_and_env(self) -> None:
        device = self.device(
            listen_port=7000,
            token_env="DOT2_EM_TOKEN",
            params={"host": "paramhost", "port": 9001},
        )
        with self.env(
            {"DOT2_HOST": "envhost", "DOT2_LISTEN_PORT": "9002", "DOT2_EM_TOKEN": "tok"}
        ):
            adapter = build(device, None)
        self.assertEqual(adapter.host, "devhost")
        self.assertEqual(adapter._link_kwargs["tls_port"], 7000)

    def test_token_env_outranks_the_secrets_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "device_token").write_text(" filetok \n", encoding="utf-8")
            device = self.device(secrets_dir=directory, token_env="DOT2_EM_TOKEN")
            with self.env({"DOT2_EM_TOKEN": "envtok"}):
                adapter = build(device, None)
        self.assertEqual(adapter._link_kwargs["token"], "envtok")

    def test_token_falls_back_to_the_secrets_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "device_token").write_text(" filetok \n", encoding="utf-8")
            device = self.device(secrets_dir=directory, token_env="DOT2_EM_TOKEN")
            with self.env():
                adapter = build(device, None)
        self.assertEqual(adapter._link_kwargs["token"], "filetok")

    def test_device_wake_is_off_unless_a_model_is_configured(self) -> None:
        with self.env():
            adapter = build(self.device(), None)
        self.assertEqual(adapter.oww_on_device, "off")
        self.assertFalse(adapter._push_oww)


if __name__ == "__main__":
    unittest.main()
