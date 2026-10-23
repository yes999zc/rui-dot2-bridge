"""Soak recorder: one JSON line a minute, for the P4 "24 h no crash" evidence.

    python scripts/soak.py --api http://127.0.0.1:8091 --minutes 720

Records what actually changes over a night: plane restarts, device
disconnects/reconnects, wake counts and the rui-side AEC state. Anything that
is supposed to survive a night (the plane, the link, memory) is a column here
rather than a claim in a doc.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path


def get(api: str, path: str) -> dict:
    with urllib.request.urlopen(f"{api}{path}", timeout=4) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8091")
    parser.add_argument("--minutes", type=float, default=720)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--out", default="dev/rui/soak.jsonl")
    parser.add_argument("--summary", default="dev/rui/soak-summary.md")
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    previous: dict[str, object] = {}
    events: list[str] = []
    samples = 0

    while time.time() < deadline:
        try:
            health = get(args.api, "/health")
            audio = get(args.api, "/api/v1/audio")
            metrics = audio.get("metrics", {})
            row = {
                "t": datetime.now().isoformat(timespec="seconds"),
                "audio_plane": health.get("audio_plane"),
                "speaker_connected": health.get("speaker_connected"),
                "plane_pid": audio.get("pid"),
                "plane_restarts": audio.get("restarts"),
                "wake_count": metrics.get("wake_count"),
                "utterances": metrics.get("utterances"),
                "gate_armed": metrics.get("gate_armed"),
                "aec_enabled": metrics.get("aec_enabled"),
                "aec_erle_db": metrics.get("aec_erle_db"),
                "rss_kb": metrics.get("rss_kb") or metrics.get("rss"),
            }
            for key in ("speaker_connected", "plane_pid", "plane_restarts"):
                if key in previous and previous[key] != row[key]:
                    events.append(f"{row['t']} {key}: {previous[key]} -> {row[key]}")
                    print(events[-1], flush=True)
                previous[key] = row[key]
            with out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            samples += 1
            print(f"{row['t']} plane={row['audio_plane']} spk={row['speaker_connected']} "
                  f"restarts={row['plane_restarts']} wakes={row['wake_count']}", flush=True)
        except Exception as exc:  # noqa: BLE001 - a soak run must record outages, not die on them
            events.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} poll failed: {type(exc).__name__}: {exc}")
            print(events[-1], flush=True)

        time.sleep(args.interval)

    summary = Path(args.summary)
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        f"# Soak · {datetime.now():%Y-%m-%d %H:%M}\n\n"
        f"- 采样 {samples} 次，跨度 {args.minutes:.0f} 分钟\n"
        f"- 状态变化 {len(events)} 条：\n\n"
        + "\n".join(f"  - {line}" for line in events[-50:])
        + "\n",
        encoding="utf-8",
    )
    print(f"summary -> {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
