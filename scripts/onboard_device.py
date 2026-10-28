"""Point an unlocked, EchoMuse-flashed Dot 2 at this deployment in one command.

The device finds its controller in exactly two ways, and only one of them is
configurable without a reflash (upstream ``device/internal/discovery/static.go``):

1. ``/data/local/etc/echomuse/controller.json`` -- an ordered endpoint list, read
   fresh on every reconnect;
2. mDNS (``_emcontroller._tcp.local``) when that file is absent.

A device that was just flashed by the EchoMuse wizard has (2) only, plus a
``ca.pem``/``token`` pair provisioned by *that* controller -- so it sits there
dialling a controller nobody answers. This script performs the three-file switch
an operator would otherwise do by hand, and then waits until rui reports the
device connected.

    python scripts/onboard_device.py \
        --device-id dot2-b \
        --rui http://192.0.2.10:8080 \
        --rui-token-file ~/.rui/console-token.txt \
        --secrets-dir ~/rui/dot2-secrets

Nothing about a real deployment (addresses, tokens, serials) is baked into this
file: every one of them is a flag. ``--dry-run`` prints the plan and touches
nothing; writing requires ``--yes``.

Exit code: 0 = device connected, 1 = a step failed, 2 = plan only (dry run, or
missing ``--yes``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ADB = os.environ.get("ADB", "adb")
DEVICE_CONF_DIR = "/data/local/etc/echomuse"
DEVICE_TMP = "/data/local/tmp"
SERVER_BIN = "/data/local/bin/server"


class StepError(RuntimeError):
    """A preflight or apply step could not be completed."""


# --------------------------------------------------------------------------- #
# adb plumbing
# --------------------------------------------------------------------------- #


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise StepError(f"{' '.join(argv)} -> exit {proc.returncode}: {proc.stderr.strip()}")
    return proc


def adb(serial: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run([ADB, "-s", serial, *args], check=check)


def device_sh(serial: str, command: str, *, check: bool = True) -> str:
    """Run ``command`` as root on the device.

    ``adb shell`` joins its remaining arguments and hands the result to the
    device's sh. Passing ``su -c '<command>'`` as ONE argument keeps the quoting
    intact; the tempting ``[..., "shell", "su", "-c", command]`` form loses it and
    silently runs the tail as uid=2000(shell), which surfaces as
    "Read-only file system" instead of a permission error.
    """
    return adb(serial, "shell", f"su -c '{command}'", check=check).stdout


def list_devices() -> list[tuple[str, str]]:
    """Return ``(serial, state)`` for every adb device, emulators excluded."""
    out = run([ADB, "devices"]).stdout.splitlines()[1:]
    found: list[tuple[str, str]] = []
    for line in out:
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("emulator-"):
            found.append((parts[0], parts[1]))
    return found


def resolve_serial(requested: str | None) -> str:
    devices = list_devices()
    online = [serial for serial, state in devices if state == "device"]
    if requested:
        if requested not in online:
            seen = ", ".join(serial for serial, _ in devices) or "none"
            raise StepError(f"serial {requested} is not an online adb device (saw: {seen})")
        return requested
    if len(online) == 1:
        return online[0]
    if not online:
        raise StepError("no online adb device: plug the Dot 2 in over USB (data cable) and retry")
    raise StepError("more than one device online, pass --serial: " + ", ".join(online))


# --------------------------------------------------------------------------- #
# rui side
# --------------------------------------------------------------------------- #


def rui_token(args: argparse.Namespace) -> str | None:
    if args.rui_token_file:
        path = Path(args.rui_token_file).expanduser()
        if not path.is_file():
            raise StepError(f"rui token file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    env_value = os.environ.get(args.rui_token_env) if args.rui_token_env else None
    return env_value.strip() if env_value else None


def api_get(base: str, path: str, token: str | None, *, timeout: float = 8.0) -> dict:
    if token is None:
        raise StepError("no rui API token: pass --rui-token-file or set RUI_TOKEN")
    req = urllib.request.Request(base.rstrip("/") + path, headers={"X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-supplied base
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise StepError(f"GET {path} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise StepError(f"GET {path} -> {exc}") from exc


@dataclass
class Plan:
    serial: str
    device_id: str
    room: str
    dial_port: int
    endpoints: list[dict[str, object]]
    files: dict[str, bytes]
    modes: dict[str, str]


def build_plan(args: argparse.Namespace) -> Plan:
    serial = resolve_serial(args.serial)
    identity = device_sh(serial, "id").strip()
    if "uid=0(root)" not in identity:
        raise StepError(f"adb root unavailable on {serial} (got {identity!r}); unlock the device first")

    token = rui_token(args)
    payload = api_get(args.rui, "/api/v2/devices", token)
    items = payload.get("items") or []
    match = next((item for item in items if item.get("id") == args.device_id), None)
    if match is None:
        known = ", ".join(str(item.get("id")) for item in items) or "none"
        raise StepError(
            f"device {args.device_id!r} is not configured in rui (known: {known}); "
            "create it in the console first so it gets a listen_port"
        )
    if not match.get("enabled", True):
        raise StepError(f"device {args.device_id!r} exists but is disabled in rui")

    # The plane serves wss on its own listen_port (adapter.py: tls_port =
    # listen_port) and a production deployment exposes no plaintext ws port, so
    # both endpoint fields point at that one port.
    dial_port = args.controller_port or int(match.get("listen_port") or 0)
    if dial_port <= 0:
        raise StepError(f"device {args.device_id!r} has no listen_port")

    host = args.controller_host or urllib.parse.urlsplit(args.rui).hostname
    if not host:
        raise StepError(f"cannot derive a controller host from --rui {args.rui!r}; pass --controller-host")

    endpoints: list[dict[str, object]] = [{"host": host, "port": dial_port, "tls_port": dial_port}]
    for extra in args.fallback or []:
        extra_host, _, extra_port = extra.rpartition(":")
        if not extra_host or not extra_port.isdigit():
            raise StepError(f"--fallback wants HOST:PORT, got {extra!r}")
        endpoints.append({"host": extra_host, "port": int(extra_port), "tls_port": int(extra_port)})

    controller = json.dumps({"endpoints": endpoints, "mdns": bool(args.mdns)}, separators=(",", ":"))
    files: dict[str, bytes] = {"controller.json": controller.encode("utf-8")}
    modes: dict[str, str] = {"controller.json": "644"}

    if args.secrets_dir:
        secrets = Path(args.secrets_dir).expanduser()
        if not args.skip_ca:
            ca = secrets / "ca.pem"
            if not ca.is_file():
                raise StepError(f"{ca} missing; need the CA the deployment's bridge pins (ca.pem)")
            files["ca.pem"] = ca.read_bytes()
            modes["ca.pem"] = "644"
        if not args.skip_token:
            token_file = secrets / "device_token"
            if not token_file.is_file():
                raise StepError(f"{token_file} missing; need the deployment's device token (device_token)")
            # A trailing newline is common in a hand-maintained secret file and
            # would become part of the X-EM-Token header.
            files["token"] = token_file.read_text(encoding="utf-8").strip().encode("utf-8")
            modes["token"] = "600"

    return Plan(
        serial=serial,
        device_id=args.device_id,
        room=str(match.get("room") or ""),
        dial_port=dial_port,
        endpoints=endpoints,
        files=files,
        modes=modes,
    )


# --------------------------------------------------------------------------- #
# apply / verify
# --------------------------------------------------------------------------- #


def apply_plan(plan: Plan, suffix: str) -> list[str]:
    notes: list[str] = []
    for name in plan.files:
        device_sh(
            plan.serial,
            f"if [ -f {DEVICE_CONF_DIR}/{name} ]; then cp {DEVICE_CONF_DIR}/{name} "
            f"{DEVICE_CONF_DIR}/{name}.bak-{suffix}; fi",
        )
    notes.append(f"backed up existing config as *.bak-{suffix}")

    # The token is a secret: stage in a 0700 dir and delete it afterwards.
    scratch = Path(tempfile.mkdtemp(prefix="onboard-dot2-"))
    try:
        os.chmod(scratch, 0o700)
        for name, blob in plan.files.items():
            local = scratch / name
            local.write_bytes(blob)
            os.chmod(local, 0o600)
            adb(plan.serial, "push", str(local), f"{DEVICE_TMP}/{name}")
        for name, mode in plan.modes.items():
            device_sh(
                plan.serial,
                f"cp {DEVICE_TMP}/{name} {DEVICE_CONF_DIR}/{name} "
                f"&& chmod {mode} {DEVICE_CONF_DIR}/{name}",
            )
        for name in plan.files:
            device_sh(plan.serial, f"rm -f {DEVICE_TMP}/{name}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    notes.append("installed " + ", ".join(sorted(plan.files)))

    for name, blob in plan.files.items():
        got = adb(plan.serial, "shell", f"su -c 'cat {DEVICE_CONF_DIR}/{name}'").stdout
        if got.encode("utf-8") != blob:
            raise StepError(f"read-back mismatch on {name} ({len(got.encode())} vs {len(blob)} bytes)")
    notes.append("read-back verified byte-for-byte")
    return notes


def server_pid(serial: str) -> int | None:
    for line in device_sh(serial, "ps", check=False).splitlines():
        if SERVER_BIN in line and "grep" not in line:
            fields = line.split()
            if len(fields) > 1 and fields[1].isdigit():
                return int(fields[1])
    return None


def restart_server(serial: str) -> None:
    pid = server_pid(serial)
    if pid is None:
        raise StepError(f"{SERVER_BIN} is not running; the start_server.sh supervisor may be down")
    device_sh(serial, f"kill -9 {pid}", check=False)


def wait_connected(args: argparse.Namespace, timeout: float) -> dict:
    token = rui_token(args)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        items = api_get(args.rui, "/api/v2/devices", token).get("items") or []
        item = next((entry for entry in items if entry.get("id") == args.device_id), None)
        if item is not None:
            state = f"{item.get('status')}/{(item.get('status_reason') or {}).get('code')}"
            if state != last:
                print(f"  {args.device_id}: {state}")
                last = state
            if item.get("connected"):
                return item
        time.sleep(2)
    raise StepError(f"{args.device_id} did not connect within {timeout:.0f}s; check the plane log")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Point a flashed Echo Dot 2 at a rui deployment's dot2 bridge.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--device-id", required=True, help="device id already configured in rui, e.g. dot2-b")
    parser.add_argument("--rui", required=True, help="rui base URL, e.g. http://192.0.2.10:8080")
    parser.add_argument("--serial", help="adb serial; auto-detected when exactly one device is online")
    parser.add_argument(
        "--controller-host",
        help="address the DEVICE should dial; defaults to the host in --rui",
    )
    parser.add_argument(
        "--controller-port",
        type=int,
        help="port to dial; defaults to the device's listen_port in rui",
    )
    parser.add_argument(
        "--fallback",
        action="append",
        metavar="HOST:PORT",
        help="extra endpoint tried after the primary one; repeatable",
    )
    parser.add_argument("--mdns", action="store_true", help="allow an mDNS attempt after every endpoint fails")
    parser.add_argument(
        "--secrets-dir",
        help="directory with ca.pem + device_token to install; omit to only rewrite controller.json",
    )
    parser.add_argument("--skip-ca", action="store_true", help="keep the device's existing ca.pem")
    parser.add_argument("--skip-token", action="store_true", help="keep the device's existing token")
    parser.add_argument("--rui-token-file", help="file holding rui's console/API token")
    parser.add_argument("--rui-token-env", default="RUI_TOKEN", help="env var holding rui's API token")
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for the device to connect")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--yes", action="store_true", help="actually write to the device")
    return parser.parse_args(argv)


def print_plan(plan: Plan) -> None:
    print("plan")
    print(f"  serial    : {plan.serial}")
    print(f"  device    : {plan.device_id} ({plan.room or 'no room'})")
    print(f"  endpoints : {json.dumps(plan.endpoints, separators=(',', ':'))}")
    for name in sorted(plan.files):
        print(f"  write     : {name} ({len(plan.files[name])} bytes, mode {plan.modes[name]})")
    print(f"  into      : {DEVICE_CONF_DIR}/")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(args)
        print_plan(plan)

        if args.dry_run:
            print("dry run: nothing written")
            return 2
        if not args.yes:
            print("refusing to write without --yes (this rewrites the device's controller config)")
            return 2

        suffix = time.strftime("%Y%m%d-%H%M%S")
        for note in apply_plan(plan, suffix):
            print(f"  {note}")
        restart_server(plan.serial)
        print("  restart sent; waiting for rui to see the device")
        item = wait_connected(args, args.timeout)
        print(
            f"connected: {plan.device_id} port={plan.dial_port} "
            f"status={item.get('status')} room={item.get('room')}"
        )
        print(f"rollback: on the device, cp {DEVICE_CONF_DIR}/<file>.bak-{suffix} over <file>")
        print("          then kill -9 the server pid; start_server.sh restarts it")
        return 0
    except StepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
