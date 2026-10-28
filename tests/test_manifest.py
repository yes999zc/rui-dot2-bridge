"""Manifest checks that do not need rui installed.

The same files are the ones rui 0.3.4 reads: plugin.yaml gates
RUI_ADAPTER_PLUGINS via device/loader.py (_module_plugin_roots looks next to the
adapter module), and deploying the identical copy under <data_dir>/plugs/ is what
the audio plane wake policy reads. When rui is importable the test also runs the
real core loader, which is the authoritative check.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml
from packaging.specifiers import SpecifierSet

import rui_dot2_bridge.adapter as adapter_module

PACKAGE = Path(adapter_module.__file__).resolve().parent


def _manifest() -> dict:
    return yaml.safe_load((PACKAGE / "plugin.yaml").read_text(encoding="utf-8"))


def _window(compat: str) -> SpecifierSet:
    """rui manifests separate comparators with spaces; PEP 440 wants commas."""
    return SpecifierSet(re.sub(r"\s+(?=[<>=!~])", ",", compat.strip()))


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
        # rui 0.6.0 keeps the same plug/adapter contract, but the manifest now uses
        # rui-device/v1 `audio_defaults`, which a pre-0.6 loader rejects
        # (DeviceManifest is extra="forbid"). 1.3.2 raises the lower bound to 0.7.0:
        # the dual window covers the 0.7.x and 0.8.x lines, and 0.6.x deployments stay
        # on 1.3.1 instead of reaching a "extra inputs are not permitted" error.
        self.assertEqual(plugin["version"], "1.3.2")
        # Dual window: 0.7.x and 0.8.x must both pass the loader's range check, so a
        # rui minor bump does not refuse the plug.
        self.assertEqual(plugin["rui_compat"], ">=0.7.0 <0.9.0")
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
        # rui 0.6 M1: the plug declares its own hardware audio defaults; they are
        # the bottom layer of manifest < global config < devices[].audio.
        defaults = device["audio_defaults"]
        self.assertEqual(
            defaults["capture"],
            {"rate": 16000, "channels": 1, "mic_channels": 1, "ref_channels": 0, "bits": 16},
        )
        self.assertEqual(defaults["playback"], {"rate": 48000, "channels": 1})
        self.assertEqual(defaults["ack_echo_ignore_ms"], 2500)
        runtime = device["runtime"]
        self.assertEqual(runtime["mode"], "in-process")
        self.assertEqual(runtime["entrypoint"], "rui_dot2_bridge.adapter:build")
        self.assertEqual(runtime["listen_port"], 8870)
        # Must name the adapter's own EchoMuse secret, not rui's transport token.
        self.assertEqual(runtime["token_env"], "DOT2_EM_TOKEN")
        self.assertEqual(
            {param["key"] for param in device["params"]},
            {
                "host",
                "room",
                "wake_words",
                "port",
                "oww_model",
                # C-001: the five formerly env-only runtime knobs are declared so
                # a v2 config can set them per device (no unknown_params_ignored).
                "control_port",
                "loudness_file",
                "eq_dir",
                "secrets_dir",
                "oww_threshold",
            },
        )
        declared = {param["key"]: param for param in device["params"]}
        self.assertEqual(
            {key: (declared[key]["type"], declared[key]["default"]) for key in
             ("control_port", "loudness_file", "eq_dir", "secrets_dir", "oww_threshold")},
            {
                "control_port": ("int", 0),
                "loudness_file": ("string", ""),
                "eq_dir": ("string", ""),
                "secrets_dir": ("string", ""),
                "oww_threshold": ("string", ""),
            },
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
            import rui
            from rui.plugins.loader import load_plugin
            from rui.plugins.models import DeviceManifest
        except Exception as exc:  # noqa: BLE001 - optional dependency
            self.skipTest(f"rui not importable here: {exc}")
        if "audio_defaults" not in getattr(DeviceManifest, "model_fields", {}):
            # The 1.2.0 manifest requires the rui 0.6 loader; an older core in this
            # interpreter cannot read rui-device/v1 audio_defaults (it rejects
            # unknown keys, by design).
            self.skipTest("installed rui predates the 0.6 audio_defaults field")
        compat = _manifest()["rui_compat"]
        if not _window(compat).contains(rui.__version__):
            # 1.3.2 ships a dual window (>=0.7.0 <0.9.0). The loader refuses an
            # out-of-range manifest by design, so a dev venv still on 0.6.x cannot
            # exercise this path; test_manifest_compat covers the range itself.
            self.skipTest(
                f"installed rui {rui.__version__} is outside the manifest window {compat}"
            )
        plugin = load_plugin(PACKAGE)
        self.assertEqual(plugin.id, "dot2-echomuse")
        self.assertEqual(str(plugin.trust), "trusted")
        self.assertEqual(len(plugin.devices), 1)
        device = plugin.devices[0]
        self.assertEqual(device.id, "dot2")
        self.assertEqual(device.wake_source.value, "device")
        self.assertTrue(device.barge_via_device_enabled)
        self.assertEqual(device.runtime.mode.value, "in-process")
        defaults = device.audio_defaults
        self.assertIsNotNone(defaults)
        self.assertIsNotNone(defaults.capture)
        self.assertIsNotNone(defaults.playback)
        self.assertEqual(
            (defaults.capture.rate, defaults.capture.channels,
             defaults.capture.mic_channels, defaults.capture.ref_channels),
            (16000, 1, 1, 0),
        )
        self.assertEqual(defaults.capture.bits, 16)
        self.assertEqual((defaults.playback.rate, defaults.playback.channels), (48000, 1))
        self.assertEqual(defaults.ack_echo_ignore_ms, 2500)


if __name__ == "__main__":
    unittest.main()
