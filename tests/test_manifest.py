"""Manifest checks that do not need rui installed.

The same files are the ones rui 0.3.4 reads: plugin.yaml gates
RUI_ADAPTER_PLUGINS via device/loader.py (_module_plugin_roots looks next to the
adapter module), and deploying the identical copy under <data_dir>/plugs/ is what
the audio plane wake policy reads. When rui is importable the test also runs the
real core loader, which is the authoritative check.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

import rui_dot2_bridge.adapter as adapter_module

PACKAGE = Path(adapter_module.__file__).resolve().parent


class ManifestLayoutTest(unittest.TestCase):
    def test_manifest_sits_next_to_the_adapter_module(self) -> None:
        # device/loader.py::_module_plugin_roots checks module_file.parent.
        self.assertTrue((PACKAGE / "plugin.yaml").is_file())
        self.assertTrue((PACKAGE / "devices" / "rs03qr.yaml").is_file())

    def test_plugin_yaml_fields(self) -> None:
        plugin = yaml.safe_load((PACKAGE / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(plugin["schema"], "rui-plugin/v0")
        # Stable id: deploy/downstream/install-plug-manifest.sh and the device
        # config params.plug_id both depend on it.
        self.assertEqual(plugin["id"], "dot2-echomuse")
        self.assertEqual(plugin["trust"], "trusted")
        # rui 0.4.x keeps the same plug/adapter contract; only the manifest gate
        # rejected 0.4.0. Widened to the next minor so 0.4.0 loads natively.
        self.assertEqual(plugin["rui_compat"], ">=0.3.0 <0.5.0")
        self.assertEqual(plugin["devices"], ["devices/rs03qr.yaml"])

    def test_device_yaml_fields(self) -> None:
        device = yaml.safe_load((PACKAGE / "devices" / "rs03qr.yaml").read_text(encoding="utf-8"))
        self.assertEqual(device["schema"], "rui-device/v1")
        self.assertEqual(device["id"], "dot2")
        self.assertEqual(device["model"], "RS03QR")
        self.assertEqual(device["class"], "B")
        self.assertEqual(device["transport"], "inbound")
        self.assertEqual(device["protocol"]["kind"], "wss")
        self.assertEqual(
            set(device["capabilities"]),
            {
                "playback",
                "led",
                "volume",
                "keys",
                "device_wake",
                "barge_via_device",
            },
        )
        self.assertEqual(device["wake"], {"source": "device", "barge_via_device": True})
        runtime = device["runtime"]
        self.assertEqual(runtime["mode"], "in-process")
        self.assertEqual(runtime["entrypoint"], "rui_dot2_bridge.adapter:build")
        self.assertEqual(runtime["listen_port"], 8870)
        # Must name the adapter's own EchoMuse secret, not rui's transport token.
        self.assertEqual(runtime["token_env"], "DOT2_EM_TOKEN")
        self.assertEqual(
            {param["key"] for param in device["params"]},
            {"host", "room", "wake_words", "port", "oww_model"},
        )
        self.assertEqual(device["compatibility"]["rui_min"], "0.3.0")

    def test_wake_block_carries_no_secret_or_address(self) -> None:
        device = yaml.safe_load((PACKAGE / "devices" / "rs03qr.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(device["wake"]), {"source", "barge_via_device"})
        rendered = str(device["wake"]).lower()
        for forbidden in ("token", "secret", "password", "192.168" + ".", "127.0.0.1"):
            self.assertNotIn(forbidden, rendered)

    def test_class_b_device_has_a_rollback_path(self) -> None:
        device = yaml.safe_load((PACKAGE / "devices" / "rs03qr.yaml").read_text(encoding="utf-8"))
        rollback = device["device_side"]["rollback"].strip()
        self.assertTrue(rollback)
        self.assertTrue(device["device_side"]["source"].startswith("https://"))

    def test_manifest_has_no_token_or_secret_values(self) -> None:
        text = (
            (PACKAGE / "plugin.yaml").read_text(encoding="utf-8")
            + (PACKAGE / "devices" / "rs03qr.yaml").read_text(encoding="utf-8")
        )
        for marker in ("X-EM-Token:", "password:", "secret:", "api_key"):
            self.assertNotIn(marker, text)


class RealLoaderTest(unittest.TestCase):
    """Run rui core load_plugin when this interpreter has rui installed."""

    def test_core_loader_accepts_the_manifest(self) -> None:
        try:
            from rui.plugins.loader import load_plugin
        except Exception as exc:  # noqa: BLE001 - optional dependency
            self.skipTest(f"rui not importable here: {exc}")
        plugin = load_plugin(PACKAGE)
        self.assertEqual(plugin.id, "dot2-echomuse")
        self.assertEqual(str(plugin.trust), "trusted")
        self.assertEqual(len(plugin.devices), 1)
        device = plugin.devices[0]
        self.assertEqual(device.id, "dot2")
        self.assertEqual(device.wake_source.value, "device")
        self.assertTrue(device.barge_via_device_enabled)
        self.assertEqual(device.runtime.mode.value, "in-process")


if __name__ == "__main__":
    unittest.main()
