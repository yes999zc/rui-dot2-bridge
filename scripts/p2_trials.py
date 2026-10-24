"""P2 trial recorder v2: watch rui's log and turn one person's attempts into numbers.

Changes from v1 (2026-10-24 first run):
  * the latency that matters is speech-end -> first audio chunk (from the VAD
    `[seg]` line), not wake -> first chunk (that one includes the command itself);
  * a wake that lands while a turn is still open (`wake while a turn was open`)
    is counted as a repeat, not as a separate attempt;
  * only trials with a transcript count toward the wake rate; wake-only and
    discarded events are reported separately;
  * mic clipping is recorded (the VAD seg peak reaches 1.0000 often);
  * `--replay` runs the same parser over a whole log, so the analysis can be
    checked without speaking into the device.

    python scripts/p2_trials.py --trials 20
    python scripts/p2_trials.py --replay dev/rui/logs/rui.log
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

TS_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")
WAKE_RE = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}.*kws: wake: (.+)$")
REPEAT_RE = re.compile(r"wake while a turn was open: (\d+) ms discarded")
CUE_RE = re.compile(r"cue: (\d+) ms of audio handed over in (\d+) ms")
SEG_RE = re.compile(r"\[seg\] ([\d.]+) s rms=([\d.]+) peak=([\d.]+) speech=([\d.]+) s")
FIRST_CHUNK_RE = re.compile(r"reply: first chunk at (\d+) Hz")
NO_SPEECH_RE = re.compile(r"wake with no speech after (\d+) ms")
BARGE_RE = re.compile(r"barge: .*silence_after_ms=(\d+)")
STOP_RE = re.compile(r"stop intent: '(.+?)' -> session closed")


@dataclass
class Trial:
    wake_at: float | None = None
    keyword: str = ""
    repeats: int = 0                 # wakes discarded because a turn was open
    cue_at: float | None = None
    speech_end_at: float | None = None
    speech_s: float = 0.0
    mic_peak: float = 0.0
    first_chunk_at: float | None = None
    no_speech_ms: int | None = None
    barge_ms: int | None = None
    transcript: str = ""
    reply: str = ""
    audio_bytes: int = 0
    error: str = ""
    stop: str = ""

    @property
    def speech_to_audio_ms(self) -> float | None:
        if self.speech_end_at is None or self.first_chunk_at is None:
            return None
        return round((self.first_chunk_at - self.speech_end_at) * 1000.0)

    @property
    def wake_to_audio_ms(self) -> float | None:
        if self.wake_at is None or self.first_chunk_at is None:
            return None
        return round((self.first_chunk_at - self.wake_at) * 1000.0)

    @property
    def status(self) -> str:
        if self.stop:
            return "stop"
        if self.transcript:
            return "ok"
        if self.no_speech_ms:
            return "no speech"
        if self.barge_ms is not None:
            return "barge"
        return "no turn"


def wallclock(line: str) -> float | None:
    match = TS_RE.search(line)
    if not match:
        return None
    hh, mm, ss, ms = (int(g) for g in match.groups())
    now = datetime.now()
    return datetime(now.year, now.month, now.day, hh, mm, ss, ms * 1000).timestamp()


def _clock(text: str) -> float | None:
    """'10:00:29' -> seconds since midnight."""
    parts = text.split(":")
    if len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return None
    return None


def parse(lines: list[str], turns: list[dict] | None = None,
          since: float | None = None) -> list[Trial]:
    """One wake line starts a trial; everything after it decorates that trial."""
    trials: list[Trial] = []
    current: Trial | None = None
    turns = list(turns or [])

    def close() -> None:
        nonlocal current
        if current is not None:
            trials.append(current)
            current = None

    for line in lines:
        stamp = wallclock(line)
        if "kws: wake:" in line:
            close()
            match = WAKE_RE.search(line)
            current = Trial(wake_at=stamp, keyword=match.group(1) if match else "")
            continue
        if current is None:
            continue
        repeat = REPEAT_RE.search(line)
        if repeat:
            current.repeats += 1
            continue
        if CUE_RE.search(line):
            current.cue_at = stamp
            continue
        seg = SEG_RE.search(line)
        if seg:
            current.speech_s = float(seg.group(4))
            current.mic_peak = float(seg.group(3))
            current.speech_end_at = stamp
            continue
        if FIRST_CHUNK_RE.search(line):
            current.first_chunk_at = stamp
            continue
        no_speech = NO_SPEECH_RE.search(line)
        if no_speech:
            current.no_speech_ms = int(no_speech.group(1))
            continue
        barge = BARGE_RE.search(line)
        if barge:
            current.barge_ms = int(barge.group(1))
            continue
        stop = STOP_RE.search(line)
        if stop:
            current.stop = stop.group(1)
            continue

    close()
    if since is not None:
        trials = [t for t in trials if t.wake_at is not None and t.wake_at >= since]
    # Attach turns by wall-clock proximity, not by order: the API returns newest
    # first (and only the last N), so zipping gave the wrong reply to the wrong
    # trial in the first replay.
    unused = list(turns)
    for trial in trials:
        if trial.first_chunk_at is None:
            continue
        chunk_clock = (datetime.fromtimestamp(trial.first_chunk_at).hour * 3600
                       + datetime.fromtimestamp(trial.first_chunk_at).minute * 60
                       + datetime.fromtimestamp(trial.first_chunk_at).second)
        best = None
        best_delta = 1e9
        for turn in unused:
            candidate = _clock(str(turn.get("ts", "")))
            if candidate is None:
                continue
            delta = abs(candidate - chunk_clock)
            if delta < best_delta:
                best, best_delta = turn, delta
        if best is not None and best_delta <= 3:
            unused.remove(best)
            trial.transcript = best.get("transcript", "")
            trial.reply = best.get("reply", "")
            trial.audio_bytes = int(best.get("audio_bytes") or 0)
            trial.error = best.get("error", "")
    return trials


def fetch_turns(api: str, limit: int = 50) -> list[dict]:
    try:
        with urllib.request.urlopen(f"{api}/api/v1/turns?limit={limit}", timeout=2) as response:
            return json.loads(response.read()).get("turns", [])
    except Exception:  # noqa: BLE001 - the recorder must survive an API hiccup
        return []


def summarise(trials: list[Trial]) -> str:
    valid = [t for t in trials if t.transcript]
    speech_lat = [t.speech_to_audio_ms for t in valid if t.speech_to_audio_ms is not None]
    wake_lat = [t.wake_to_audio_ms for t in trials if t.wake_to_audio_ms is not None]
    clipped = [t for t in trials if t.mic_peak >= 0.999]
    barges = [t.barge_ms for t in trials if t.barge_ms is not None]
    parts = [
        f"trial={len(trials)} 出话={len(valid)}",
        f"说完→出声 P50={statistics.median(speech_lat):.0f} ms" if speech_lat else "说完→出声 —",
        f"P90={statistics.quantiles(speech_lat, n=10)[-1]:.0f} ms" if len(speech_lat) > 2 else "",
        f"唤醒→出声 P50={statistics.median(wake_lat):.0f} ms" if wake_lat else "",
        f"麦克风削波={len(clipped)}/{len(trials)}",
        f"barge={len(barges)}",
    ]
    return " · ".join(p for p in parts if p)


def render(trials: list[Trial]) -> str:
    rows = "\n".join(
        f"| {i + 1} | {t.keyword} | {t.speech_to_audio_ms if t.speech_to_audio_ms is not None else '—'} | "
        f"{t.wake_to_audio_ms if t.wake_to_audio_ms is not None else '—'} | {t.status} | "
        f"{t.transcript or '—'} | {(t.reply[:26] + '…') if len(t.reply) > 26 else (t.reply or '—')} | "
        f"{t.repeats or '—'} | {t.mic_peak:.2f} |"
        for i, t in enumerate(trials)
    )
    return ("| # | 唤醒词 | 说完→出声 (ms) | 唤醒→出声 (ms) | 结果 | 听到的 | 回答 | 重复丢弃 | mic峰值 |\n"
            "|---|---|---|---|---|---|---|---|---|\n" + (rows or "| — | — | — | — | — | — | — | — | — |"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="dev/rui/logs/rui.log")
    parser.add_argument("--api", default="http://127.0.0.1:8091")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--out", default="docs/measurements.md")
    parser.add_argument("--jsonl", default="dev/rui/p2-trials.jsonl")
    parser.add_argument("--replay", help="parse this finished log instead of watching")
    args = parser.parse_args()

    if args.replay:
        lines = Path(args.replay).read_text(encoding="utf-8", errors="replace").splitlines()
        trials = parse(lines, fetch_turns(args.api))
        print(summarise(trials) + "\n\n" + render(trials))
        return 0

    log_path = Path(args.log)
    if not log_path.exists():
        raise SystemExit(f"log not found: {log_path} (start rui first)")
    since = datetime.now().timestamp() - 1.0
    print(f"watching {log_path} · target {args.trials} answered turns · Ctrl-C to stop")
    try:
        while True:
            time.sleep(0.5)
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            trials = parse(lines, fetch_turns(args.api), since=since)
            answered = sum(1 for t in trials if t.transcript)
            print(f"\r唤醒={len(trials)} 出话={answered} 目标={args.trials}", end="", flush=True)
            if answered >= args.trials:
                print()
                break
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        trials = parse(lines, fetch_turns(args.api), since=since)
        summary = summarise(trials)
        table = render(trials)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(f"\n### P2 真人实测 · {datetime.now():%Y-%m-%d %H:%M}\n\n{summary}\n\n{table}\n")
        with Path(args.jsonl).open("a", encoding="utf-8") as handle:
            for trial in trials:
                record = dict(trial.__dict__)
                record["speech_to_audio_ms"] = trial.speech_to_audio_ms
                record["wake_to_audio_ms"] = trial.wake_to_audio_ms
                record["status"] = trial.status
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print("\n" + summary + "\n\n" + table)
        print(f"\nappended to {out} and {args.jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
