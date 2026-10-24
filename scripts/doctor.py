"""Preflight for a dot2 deployment: says PASS/WARN/FAIL before anything is touched.

Runs the same checks on this Mac and on the NAS (everything is a path or a URL):

    python scripts/doctor.py --config dev/rui/config.yaml --secrets secrets \
        --api http://127.0.0.1:8091 [--device]

It reads the device over adb only when ``--device`` is passed (the NAS will not
have it). Exit code: 0 = no FAIL, 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ADB = os.environ.get("ADB", "adb")
#: The deployment's own ceiling; 30 % was the overnight rule, 70 % is normal.
#: The deployment's ceiling (2026-10-24: 100 % = unity; the old 30 % was -44.5 dB).
VOLUME_CAP = float(os.environ.get("DOT2_VOLUME_CAP", "100") or 100)
SERIAL = os.environ.get("DOT2_SERIAL", "G090LF0964340RN6")
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str) -> None:
    results.append((status, name, detail))


def check(condition: bool, name: str, ok: str, fail: str) -> bool:
    record("PASS" if condition else "FAIL", name, ok if condition else fail)
    return condition


def warn(condition: bool, name: str, ok: str, bad: str) -> None:
    record("PASS" if condition else "WARN", name, ok if condition else bad)


def secret(name: str) -> str | None:
    env = Path.home() / ".hermes" / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(name)


def adb(*args: str, timeout: float = 15.0) -> str:
    try:
        out = subprocess.run(
            [ADB, "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout
        )
        return out.stdout or out.stderr
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return f"<adb failed: {exc}>"


def check_certs(secrets_dir: Path) -> None:
    for name in ("ca.pem", "server.pem", "server.key"):
        path = secrets_dir / name
        if not check(path.is_file(), f"cert {name}", f"present ({path})", f"missing: {path}"):
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        warn(mode & 0o077 == 0, f"perm {name}", f"{oct(mode)} (private)", f"{oct(mode)}: chmod 600")
    token = secrets_dir / "device_token"
    warn(token.is_file(), "device token", "present", f"missing: {token} (X-EM-Token checks stay open)")
    if token.is_file():
        mode = stat.S_IMODE(token.stat().st_mode)
        warn(mode & 0o077 == 0, "perm device_token", f"{oct(mode)} (private)", f"{oct(mode)}: chmod 600")


def check_config(path: Path) -> dict:
    text = path.read_text()
    try:
        sys.path.insert(0, str(Path("01_Code/projects/house-ear/src").resolve()))
        from house_ear.config import load_config

        cfg = load_config(str(path))
        dev = next((d for d in cfg.devices if d.adapter == "dot2"), None)
        check(dev is not None, "config device", f"dot2 @ {dev.host}:{dev.listen_port}", "no adapter: dot2 device")
        if dev is not None:
            check(int(dev.volume) <= VOLUME_CAP, "config volume",
                  f"{dev.volume}% (≤{VOLUME_CAP:.0f}%)", f"{dev.volume}% > {VOLUME_CAP:.0f}%")
        capture = cfg.audio.capture
        check(capture.rate == 16000, "capture rate", "16000 Hz", f"{capture.rate} Hz")
        check(capture.bits == 16, "capture bits", "S16", f"{capture.bits} bit")
        check(
            (capture.channels, capture.mic_channels, capture.ref_channels) in ((1, 1, 0), (2, 1, 1)),
            "capture layout",
            f"{capture.channels}ch = {capture.mic_channels} mic + {capture.ref_channels} ref",
            "expected 1/1/0 (mono) or 2/1/1 (P3 reference)",
        )
        check(cfg.audio.playback.rate == 48000, "playback rate", "48000 Hz", f"{cfg.audio.playback.rate} Hz")
        keywords = Path(cfg.audio.voice.kws_keywords_file)
        check(keywords.is_file(), "keywords file", str(keywords), f"missing: {keywords}")
        if keywords.is_file():
            check("你好小瑞" in keywords.read_text(), "wake word", "你好小瑞 present", "wake word missing")
    except Exception as exc:  # noqa: BLE001
        warn(False, "config semantic check", "", f"could not use house_ear loader: {exc}")
    warn("adapter: dot2" in text, "config adapter", "adapter: dot2", "config does not mention adapter: dot2")
    return {"text": text}


def check_ports(host: str, port: int) -> None:
    busy = False
    with socket.socket() as sock:
        sock.settimeout(0.4)
        busy = sock.connect_ex((host, port)) == 0
    warn(busy, f"port {port}", "listening (adapter up)", "free (adapter not running yet)")


def check_api(api: str) -> None:
    try:
        with urllib.request.urlopen(f"{api}/health", timeout=3) as response:
            health = json.loads(response.read())
        record("PASS", "rui health", f"{api} → {health.get('status')}")
        warn(health.get("audio_plane") is True, "audio plane", "running", "not running")
        warn(health.get("speaker_connected") is True, "device link", "connected", "device not connected")
        with urllib.request.urlopen(f"{api}/api/v1/device/volume", timeout=3) as response:
            volume = json.loads(response.read())
        value = volume.get("volume")
        if isinstance(value, int):
            ceiling = round(VOLUME_CAP * 255 / 100)
            check(value <= ceiling, "device volume",
                  f"{value}/255 (≤{VOLUME_CAP:.0f}%)", f"{value}/255 > {VOLUME_CAP:.0f}% ({ceiling})")
        else:
            warn(False, "device volume", "visible", "not reported by the adapter")
    except Exception as exc:  # noqa: BLE001
        record("WARN", "rui health", f"{api} unreachable: {type(exc).__name__}")


def check_device(expect_tls: int) -> None:
    controllers = adb("shell", "su -c 'cat /data/local/etc/echomuse/controller.json'")
    warn(f'"tls_port":{expect_tls}' in controllers.replace(" ", ""),
         "device endpoint", f"controller.json points at tls_port {expect_tls}",
         f"controller.json does not mention tls_port {expect_tls}: {controllers.strip()[:120]}")
    log = adb("shell", "su -c 'cat /tmp/server.log'")
    lines = [line for line in log.splitlines() if "Registered" in line or "Volume set" in line
             or "[mic] clock" in line or "[aec] enabled" in line or "[aec] disabled" in line]
    tail = lines[-6:]
    record("PASS", "device log", " | ".join(tail) if tail else "no recent lines")
    check("Volume set to 38/127" in log, "device volume push", "38/127 seen", "38/127 not seen in the log")
    check("[aec] enabled" in log, "device AEC", "enabled", "not enabled (P3 test mode?)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="dev/rui/config.yaml")
    parser.add_argument("--secrets", default="secrets")
    parser.add_argument("--api", default="http://127.0.0.1:8091")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8870)
    parser.add_argument("--device", action="store_true", help="also inspect the Dot over adb")
    args = parser.parse_args()

    secret("XFYUN_API_KEY")  # no-op today, keeps the helper honest about env sourcing
    check_certs(Path(args.secrets))
    check_config(Path(args.config))
    check_ports(args.host, args.port)
    check_api(args.api)
    if args.device:
        check_device(args.port)

    width = max(len(name) for _, name, _ in results)
    for status, name, detail in results:
        mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[status]
        print(f"{mark} {name:<{width}}  {detail}")
    fails = sum(1 for status, _, _ in results if status == "FAIL")
    warns = sum(1 for status, _, _ in results if status == "WARN")
    print(f"\n{len(results)} checks · {fails} fail · {warns} warn")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
