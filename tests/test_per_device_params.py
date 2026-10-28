"""C-001: the five dot2 runtime knobs resolve per device, not per host.

One audio plane per dot2 on a shared host means the loudness state file and the
loopback control port must not be global. Every test scrubs ``DOT2_*`` from the
environment first, so it passes on a developer box that still exports the
single-device compose values and on CI alike.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import rui_dot2_bridge.adapter as adapter_module
from rui_dot2_bridge.adapter import Dot2Adapter, build


def clean_env(extra: dict | None = None):
    """patch.dict with every DOT2_* export removed, then ``extra`` applied."""
    values = {key: value for key, value in os.environ.items() if not key.startswith("DOT2_")}
    if extra:
        values.update(extra)
    return mock.patch.dict(os.environ, values, clear=True)


def device(**overrides) -> SimpleNamespace:
    """A DeviceConfig fake: no ``secrets_dir`` field, exactly like rui's schema."""
    values = {
        "id": "dot2",
        "host": "127.0.0.1",
        "room": "",
        "listen_port": None,
        "token_env": None,
        "params": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.loads(response.read())


class _FakeLink:
    """open() only starts the link and reads bound_ports; close() stops it."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.bound_ports = {"tls": kwargs.get("tls_port")}
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class DeviceDefaultsTest(unittest.TestCase):
    """Acceptance 1: two devices, no env -> isolated state, shared EQ."""

    def test_two_devices_get_isolated_loudness_files_and_shared_eq(self) -> None:
        with clean_env():
            first = build(device(id="dot2-a"), None)
            second = build(device(id="dot2-b"), None)

        self.assertEqual(first._loudness_file, Path("dev/rui/dot2-a/loudness.json"))
        self.assertEqual(second._loudness_file, Path("dev/rui/dot2-b/loudness.json"))
        self.assertNotEqual(first._loudness_file, second._loudness_file)
        self.assertEqual(first._control_port, 0)
        self.assertEqual(second._control_port, 0)
        # Read-only vendor curves stay shared: splitting them would lose EQ_*.cfg.
        self.assertEqual(first._eq_dir, second._eq_dir)
        self.assertIsNone(first.oww_threshold)
        # No per-device secrets dir on this host -> the shared one, as in 0.7.9.
        self.assertEqual(first._link_kwargs["certs_dir"], Path("secrets"))
        self.assertEqual(second._link_kwargs["certs_dir"], Path("secrets"))


class ResolutionOrderTest(unittest.TestCase):
    """Acceptance 2: params > env > default for all five keys."""

    def test_params_outrank_env_for_all_five_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            params_secrets = Path(directory, "secrets-a")
            params_secrets.mkdir()
            (params_secrets / "device_token").write_text("token-from-params", encoding="utf-8")
            env_secrets = Path(directory, "secrets-env")
            env_secrets.mkdir()
            (env_secrets / "device_token").write_text("token-from-env", encoding="utf-8")

            dev = device(
                id="dot2-a",
                token_env="DOT2_TOK_A",
                params={
                    "control_port": 9101,
                    "loudness_file": "state/a-loudness.json",
                    "eq_dir": "state/a-eq",
                    "secrets_dir": str(params_secrets),
                    "oww_threshold": "0.62",
                },
            )
            with clean_env(
                {
                    "DOT2_CONTROL_PORT": "9202",
                    "DOT2_LOUDNESS_FILE": "state/env-loudness.json",
                    "DOT2_EQ_DIR": "state/env-eq",
                    "DOT2_SECRETS_DIR": str(env_secrets),
                    "DOT2_OWW_THRESHOLD": "0.41",
                }
            ):
                adapter = build(dev, None)

        self.assertEqual(adapter._control_port, 9101)
        self.assertEqual(adapter._loudness_file, Path("state/a-loudness.json"))
        self.assertEqual(adapter._eq_dir, Path("state/a-eq"))
        self.assertEqual(adapter._link_kwargs["certs_dir"], params_secrets)
        self.assertEqual(adapter.oww_threshold, 0.62)
        # device_token comes from the resolved (params) directory, not the env one.
        self.assertEqual(adapter._link_kwargs["token"], "token-from-params")

    def test_env_outranks_the_device_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_secrets = Path(directory)
            (env_secrets / "device_token").write_text("token-from-env-dir", encoding="utf-8")
            with clean_env(
                {
                    "DOT2_CONTROL_PORT": "9202",
                    "DOT2_LOUDNESS_FILE": "state/env-loudness.json",
                    "DOT2_EQ_DIR": "state/env-eq",
                    "DOT2_SECRETS_DIR": str(env_secrets),
                    "DOT2_OWW_THRESHOLD": "0.41",
                }
            ):
                adapter = build(device(id="dot2-c"), None)

        self.assertEqual(adapter._control_port, 9202)
        self.assertEqual(adapter._loudness_file, Path("state/env-loudness.json"))
        self.assertEqual(adapter._eq_dir, Path("state/env-eq"))
        self.assertEqual(adapter._link_kwargs["certs_dir"], env_secrets)
        self.assertEqual(adapter.oww_threshold, 0.41)
        self.assertEqual(adapter._link_kwargs["token"], "token-from-env-dir")

    def test_explicit_control_port_zero_is_not_treated_as_unset(self) -> None:
        # 0 means "control surface off" and must beat a global DOT2_CONTROL_PORT.
        with clean_env({"DOT2_CONTROL_PORT": "9202"}):
            adapter = build(device(id="dot2-d", params={"control_port": 0}), None)
        self.assertEqual(adapter._control_port, 0)


class CtorFallbackTest(unittest.TestCase):
    """Acceptance 2 (ctor half): None falls back to env, explicit values win."""

    def test_ctor_uses_explicit_values_over_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with clean_env(
                {
                    "DOT2_EQ_DIR": "env/eq",
                    "DOT2_LOUDNESS_FILE": "env/loudness.json",
                    "DOT2_CONTROL_PORT": "9304",
                    "DOT2_OWW_THRESHOLD": "0.44",
                }
            ):
                adapter = Dot2Adapter(
                    "dot2",
                    host="127.0.0.1",
                    secrets_dir=directory,
                    eq_dir="explicit/eq",
                    loudness_file="explicit/loudness.json",
                    control_port=9303,
                    oww_threshold="0.55",
                )
        self.assertEqual(adapter._eq_dir, Path("explicit/eq"))
        self.assertEqual(adapter._loudness_file, Path("explicit/loudness.json"))
        self.assertEqual(adapter._control_port, 9303)
        self.assertEqual(adapter.oww_threshold, 0.55)

    def test_ctor_falls_back_to_env_only_when_none(self) -> None:
        with clean_env(
            {
                "DOT2_EQ_DIR": "env/eq",
                "DOT2_LOUDNESS_FILE": "env/loudness.json",
                "DOT2_CONTROL_PORT": "9304",
                "DOT2_OWW_THRESHOLD": "0.44",
            }
        ):
            adapter = Dot2Adapter("dot2", host="127.0.0.1", secrets_dir="secrets")
        self.assertEqual(adapter._eq_dir, Path("env/eq"))
        self.assertEqual(adapter._loudness_file, Path("env/loudness.json"))
        self.assertEqual(adapter._control_port, 9304)
        self.assertEqual(adapter.oww_threshold, 0.44)


class SecretsIsolationTest(unittest.TestCase):
    """Acceptance 4: params.secrets_dir, per-device token_env, own device_token."""

    def test_each_device_reads_its_own_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_dir = Path(directory, "first")
            first_dir.mkdir()
            (first_dir / "device_token").write_text("token-a-file", encoding="utf-8")
            second_dir = Path(directory, "second")
            second_dir.mkdir()
            (second_dir / "device_token").write_text("token-b-file", encoding="utf-8")
            first = device(
                id="dot2-a", token_env="DOT2_TOK_A", params={"secrets_dir": str(first_dir)}
            )
            second = device(
                id="dot2-b", token_env="DOT2_TOK_B", params={"secrets_dir": str(second_dir)}
            )
            with clean_env({"DOT2_TOK_A": "token-a-env"}):
                adapter_first = build(first, None)
                adapter_second = build(second, None)

        self.assertEqual(adapter_first._link_kwargs["certs_dir"], first_dir)
        self.assertEqual(adapter_second._link_kwargs["certs_dir"], second_dir)
        # token_env wins when it is set for that device; otherwise its own file.
        self.assertEqual(adapter_first._link_kwargs["token"], "token-a-env")
        self.assertEqual(adapter_second._link_kwargs["token"], "token-b-file")

    def test_an_existing_per_device_secrets_dir_is_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "secrets" / "dot2-z").mkdir(parents=True)
            (root / "secrets" / "dot2-z" / "device_token").write_text(
                "token-per-device", encoding="utf-8"
            )
            (root / "secrets" / "device_token").write_text("token-shared", encoding="utf-8")
            previous = Path.cwd()
            os.chdir(root)
            try:
                with clean_env():
                    adapter = build(device(id="dot2-z"), None)
            finally:
                os.chdir(previous)

        self.assertEqual(adapter._link_kwargs["certs_dir"], Path("secrets/dot2-z"))
        self.assertEqual(adapter._link_kwargs["token"], "token-per-device")


class ControlPortContentionTest(unittest.IsolatedAsyncioTestCase):
    """Acceptance 3: a taken port degrades one plane, it does not abort open()."""

    async def test_second_plane_opens_without_the_control_surface(self) -> None:
        port = free_port()
        first = build(device(id="dot2-a", params={"control_port": port}), None)
        second = build(device(id="dot2-b", params={"control_port": port}), None)
        self.addAsyncCleanup(first.close)
        self.addAsyncCleanup(second.close)

        with mock.patch.object(adapter_module, "DeviceLink", _FakeLink):
            await first.open()
            self.assertIsNotNone(first._control_server)
            with self.assertLogs("rui_dot2_bridge.adapter", level="WARNING") as captured:
                await second.open()

        self.assertIsNone(second._control_server)
        # The plane itself (inbound link) still started; only the tuning API fell away.
        self.assertIsNotNone(second._link)
        self.assertEqual(second._link.started, 1)
        self.assertTrue(
            any(f"127.0.0.1:{port}" in line and "unavailable" in line for line in captured.output),
            captured.output,
        )
        # The first plane's control surface is still serving.
        health = await self._health(port)
        self.assertTrue(health["ok"])

    async def _health(self, port: int) -> dict:
        return await asyncio.to_thread(get_json, port, "/health")


class OpenFailureCleanupTest(unittest.IsolatedAsyncioTestCase):
    """A failure after the link started must not leak that listener."""

    async def test_inject_bind_failure_closes_the_inbound_link(self) -> None:
        with clean_env():
            adapter = Dot2Adapter(
                "dot2",
                host="127.0.0.1",
                secrets_dir="secrets",
                inject_port=free_port(),
                control_port=0,
            )
        with (
            mock.patch.object(adapter_module, "DeviceLink", _FakeLink),
            mock.patch(
                "rui_dot2_bridge.inject.start_inject_server", side_effect=OSError("in use")
            ),
        ):
            with self.assertRaises(OSError):
                await adapter.open()

        self.assertIsNone(adapter._link)
        self.assertIsNone(adapter._inject_server)
        self.assertIsNone(adapter._control_server)
        self.assertTrue(adapter._closed)


if __name__ == "__main__":
    unittest.main()
