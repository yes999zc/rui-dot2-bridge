"""Device-side health snapshot from /tmp/server.log (read-only, over adb).

    python scripts/device_health.py [--serial G090LF0964340RN6] [--json]

Reports the numbers the firmware itself keeps: mic clock continuity (stalls,
sub_drops, skew), the last AEC state, volume, RSS/goroutines, and whether the
current controller is ours. That last line is how a morning check tells
"the Dot is healthy and talking to rui" from "it silently fell back to the old
controller".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

ADB = os.environ.get("ADB", "adb")


def pull_log(serial: str) -> str:
    try:
        out = subprocess.run(
            [ADB, "-s", serial, "shell", "su -c 'cat /tmp/server.log'"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"adb failed: {exc}") from exc


def last(pattern: str, text: str) -> re.Match[str] | None:
    matches = list(re.finditer(pattern, text))
    return matches[-1] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("DOT2_SERIAL", "G090LF0964340RN6"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--port", type=int, default=8870)
    args = parser.parse_args()

    text = pull_log(args.serial)
    report: dict[str, object] = {"serial": args.serial, "problems": []}

    clock = last(r"\[mic\] clock: ([\d.]+)s audio over ([\d.]+)s wall \(skew (-?\d+)ms capture fast, stalls=(\d+), sub_drops=(\d+)\)", text)
    if clock:
        report["mic_clock"] = {
            "audio_s": float(clock.group(1)), "wall_s": float(clock.group(2)),
            "skew_ms": int(clock.group(3)), "stalls": int(clock.group(4)),
            "sub_drops": int(clock.group(5)),
        }
        if int(clock.group(4)) or int(clock.group(5)):
            report["problems"].append("mic stream stalls/sub_drops > 0")

    mem = last(r"\[mem\] goroutines=(\d+) heap_alloc=(\d+)KB heap_sys=(\d+)KB .* rss=(\d+)KB", text)
    if mem:
        report["memory"] = {"goroutines": int(mem.group(1)), "rss_kb": int(mem.group(4))}

    aec = last(r"\[aec\] (enabled: frame=(\d+) tail=(\d+)ms delay=(\d+)ms|disabled)", text)
    if aec:
        report["aec"] = "enabled" if aec.group(1).startswith("enabled") else "disabled"
        if aec.group(2):
            report["aec_frame"], report["aec_tail_ms"] = int(aec.group(2)), int(aec.group(3))

    volume = last(r"Volume set to (\d+)/(\d+)", text)
    if volume:
        report["volume"] = {"level": int(volume.group(1)), "max": int(volume.group(2))}
        if int(volume.group(1)) > 38:
            report["problems"].append(f"volume {volume.group(1)}/127 above the 30% ceiling")

    registered = last(r"\[control\] Registered as (\S+) \(version (\S+)\)", text)
    if registered:
        report["registered"] = {"device_id": registered.group(1), "version": registered.group(2)}

    connected_to_us = last(r"Connecting to wss://([\d.]+):(\d+)", text)
    if connected_to_us:
        report["endpoint"] = f"wss://{connected_to_us.group(1)}:{connected_to_us.group(2)}"
        if int(connected_to_us.group(2)) != args.port:
            report["problems"].append(f"talking to port {connected_to_us.group(2)}, not our adapter {args.port}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for key, value in report.items():
            if key != "problems":
                print(f"{key:12s} {value}")
        print(f"{'problems':12s} {report['problems'] or 'none'}")
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
