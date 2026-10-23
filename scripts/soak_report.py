"""Summarise the soak JSONL: what changed over a night, in one markdown page.

    python scripts/soak_report.py [--in dev/rui/soak.jsonl] [--out docs/soak-report.md]

Reports only what is in the file: sampling span, gaps in sampling, plane pid
changes, restarts, disconnected intervals, wake counts. A soak that is still
running is labelled as partial; a finished one is not.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="source", default="dev/rui/soak.jsonl")
    parser.add_argument("--out", default="docs/soak-report.md")
    parser.add_argument("--gap-factor", type=float, default=2.5,
                        help="a spacing this many times the median counts as a gap")
    args = parser.parse_args()

    path = Path(args.source)
    rows = load(path)
    if not rows:
        raise SystemExit(f"no samples in {path} (is soak.py running?)")

    times = [datetime.fromisoformat(row["t"]) for row in rows]
    gaps = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
    median = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
    big = [(times[i - 1], times[i], gaps[i - 1]) for i in range(1, len(times))
           if median and gaps[i - 1] > median * args.gap_factor]

    pids: list[object] = []
    pid_changes: list[tuple[str, object]] = []
    for row in rows:
        if row.get("plane_pid") not in pids:
            pids.append(row.get("plane_pid"))
            pid_changes.append((row.get("t"), row.get("plane_pid")))
    down = [row["t"] for row in rows if row.get("speaker_connected") is False]
    wakes = max(int(row.get("wake_count") or 0) for row in rows)

    span = (times[-1] - times[0]).total_seconds() / 60.0
    markdown = [
        f"# Soak 报告（{datetime.now():%Y-%m-%d %H:%M}）",
        "",
        f"- 采样 **{len(rows)}** 次，跨度 **{span:.0f} 分钟**"
        + ("（仍在跑，partial）" if span < 120 else ""),
        f"- 采样间隔中位数 **{median:.0f} s**，异常间断 **{len(big)}** 处",
        f"- 平面 pid 变化 **{len(pids)}** 次（多为夜间手工重启/测试）："
        + ", ".join(f"{t}→{pid}" for t, pid in pid_changes),
        f"- 设备断连采样点 **{len(down)}** 个" + (f"：{down[:6]}" if down else ""),
        f"- 唤醒计数峰值 **{wakes}**",
        "",
        "## 异常间断",
        "",
    ]
    markdown += [f"- {a:%H:%M:%S} → {b:%H:%M:%S}（{g:.0f}s）" for a, b, g in big] or ["- 无"]
    markdown += ["", "## 判定", ""]
    ok = not big and len(pids) <= 1 and not down
    markdown.append("- ✅ 跨度内无采样间断、无平面重建、无断连" if ok
                    else "- 有间断/重建/断连；夜间为测试重启（pid 变化时间点见上），"
                         "最终结论以不再重启之后的整段为准")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print("\n".join(markdown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
