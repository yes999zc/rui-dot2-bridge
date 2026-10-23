"""P2 trial recorder: watch rui's log and turn one person's attempts into numbers.

Run it while you speak to the Dot; it pairs each wake with the turn it produced
and prints a table plus a JSONL record.

    ./scripts/dev_rui.sh &          # or however rui is started
    python scripts/p2_trials.py --trials 20

Measured per trial:
  * wake -> first audible chunk (wall clock, ms)   <- the user-facing latency
  * transcript / reply / audio bytes (from /api/v1/turns)
  * wake with no speech (a miss), and barge events (from the plane log)

Why the log and not the API: the wake instant only exists in the plane log, and
the "reply: first chunk" line is the first moment sound is on the wire. The API
is polled for the transcript because the log does not carry it.

Output:
  * docs/measurements.md   - append-ready markdown table + summary
  * dev/rui/p2-trials.jsonl - raw events, one JSON object per line
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WAKE_RE = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}.*wake: (.+)$")
FIRST_CHUNK_RE = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}.*reply: first chunk at (\d+) Hz")
NO_SPEECH_RE = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}.*wake with no speech after (\d+) ms")
BARGE_RE = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}.*barge: .*silence_after_ms=(\d+)")
TS_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")


@dataclass
class Trial:
    started: float                      # wall clock at the wake line
    keyword: str = ""
    first_chunk_at: float | None = None
    no_speech_ms: int | None = None
    barge_ms: int | None = None
    transcript: str = ""
    reply: str = ""
    audio_bytes: int = 0
    error: str = ""

    @property
    def latency_ms(self) -> float | None:
        if self.first_chunk_at is None:
            return None
        return round((self.first_chunk_at - self.started) * 1000.0, 1)


def wallclock(line: str, last: float) -> float:
    """Timestamp of a log line as local wall clock, tolerant of a date-less prefix."""
    match = TS_RE.search(line)
    if not match:
        return time.time()
    hh, mm, ss, ms = (int(g) for g in match.groups())
    now = datetime.now()
    return datetime(now.year, now.month, now.day, hh, mm, ss, ms * 1000).timestamp()


def fetch_turns(api: str, limit: int = 5) -> list[dict]:
    try:
        with urllib.request.urlopen(f"{api}/api/v1/turns?limit={limit}", timeout=2) as response:
            return json.loads(response.read()).get("turns", [])
    except Exception:  # noqa: BLE001 - the recorder must survive an API hiccup
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="dev/rui/logs/rui.log")
    parser.add_argument("--api", default="http://127.0.0.1:8091")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--out", default="docs/measurements.md")
    parser.add_argument("--jsonl", default="dev/rui/p2-trials.jsonl")
    parser.add_argument("--idle-timeout", type=float, default=45.0,
                        help="stop after this long without a wake")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        raise SystemExit(f"log not found: {log_path} (start rui first)")

    trials: list[Trial] = []
    current: Trial | None = None
    last_event = time.time()
    known_turns: set[str] = {t.get("ts", "") for t in fetch_turns(args.api)}
    jsonl = Path(args.jsonl)
    jsonl.parent.mkdir(parents=True, exist_ok=True)

    print(f"watching {log_path} · target {args.trials} trials · Ctrl-C to stop early")
    position = log_path.stat().st_size
    try:
        while True:
            time.sleep(0.15)
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                new = handle.read()
                position = handle.tell()
            if not new:
                now = time.time()
                if current is not None and current.first_chunk_at is not None and now - last_event > 3:
                    trials.append(current)
                    current = None
                    print(f"trial {len(trials):2d}: {trials[-1].latency_ms} ms")
                    if len(trials) >= args.trials:
                        break
                if now - last_event > args.idle_timeout and not trials:
                    print("idle timeout with no wake yet; still watching (Ctrl-C to stop)")
                    last_event = now
                continue

            for line in new.splitlines():
                if WAKE_RE.search(line):
                    if current is not None:
                        trials.append(current)
                    current = Trial(started=wallclock(line, last_event), keyword=WAKE_RE.search(line).group(1))
                    last_event = time.time()
                    continue
                if FIRST_CHUNK_RE.search(line) and current is not None:
                    current.first_chunk_at = wallclock(line, last_event)
                    last_event = time.time()
                    continue
                match = NO_SPEECH_RE.search(line)
                if match and current is not None:
                    current.no_speech_ms = int(match.group(1))
                    continue
                match = BARGE_RE.search(line)
                if match and trials:
                    trials[-1].barge_ms = int(match.group(1))
                    continue

            # Attach the newest turn to the freshest trial that has audio.
            if current is not None and current.first_chunk_at is not None:
                for turn in fetch_turns(args.api):
                    if turn.get("ts") in known_turns:
                        continue
                    known_turns.add(turn.get("ts", ""))
                    current.transcript = turn.get("transcript", "")
                    current.reply = turn.get("reply", "")
                    current.audio_bytes = int(turn.get("audio_bytes") or 0)
                    current.error = turn.get("error", "")
                    break
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if current is not None:
            trials.append(current)

    latencies = [t.latency_ms for t in trials if t.latency_ms is not None]
    spoke = [t for t in trials if t.transcript]
    rows = "\n".join(
        f"| {i + 1} | {t.keyword} | {t.latency_ms if t.latency_ms is not None else '—'} | "
        f"{t.transcript or ('no speech' if t.no_speech_ms else '—')} | "
        f"{(t.reply[:24] + '…') if len(t.reply) > 24 else (t.reply or '—')} | "
        f"{t.barge_ms if t.barge_ms is not None else '—'} |"
        for i, t in enumerate(trials)
    )
    summary = (
        f"n={len(trials)}, 出话={len(spoke)}, 唤醒→首帧 P50="
        f"{statistics.median(latencies) if latencies else '—'} ms, P90="
        f"{statistics.quantiles(latencies, n=10)[-1] if len(latencies) > 2 else '—'} ms"
    )
    table = (
        "| # | 唤醒词 | 唤醒→首帧 (ms) | 听到的 | 回答 | 打断 (ms) |\n"
        "|---|---|---|---|---|---|\n" + (rows or "| — | — | — | — | — | — |")
    )
    out = Path(args.out)
    header = f"\n### P2 真人实测 · {datetime.now():%Y-%m-%d %H:%M}\n\n{summary}\n\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(header + table + "\n")
    with jsonl.open("a", encoding="utf-8") as handle:
        for trial in trials:
            handle.write(json.dumps(trial.__dict__, ensure_ascii=False) + "\n")

    print("\n" + summary + "\n\n" + table)
    print(f"\nappended to {out} and {jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
