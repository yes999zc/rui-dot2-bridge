"""Unattended end-to-end check: wake -> command -> turn, without a person.

It plays a wake word and a command through the Dot's own speaker (the only
speaker in the room) and watches the rui instance for the wake, the transcript
and the reply. Timing is driven by the live gate state, not by sleeps: the
earlier hand-run attempts failed because the command landed outside the 5 s
wake window, not because the audio was wrong.

Requires rui started with the test hooks on (AEC off for the loop to hear
itself, an inject port, and a recording path for the audit trail):

    DOT2_AEC_ENABLED=0 DOT2_INJECT_PORT=8871 DOT2_RECORD=/tmp/e2e.wav ./scripts/dev_rui.sh
    python tools/self_test_voice.py --wake /tmp/xf-wake.wav --command /tmp/xf-cmd.wav

Exit code 0 only when a turn with a non-empty transcript and reply was recorded.
This is a smoke test, not the P2 benchmark: one synthetic voice, one attempt,
no statistics.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.request


def inject(port: int, wav: str) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.sendall((json.dumps({"wav": wav}) + "\n").encode())
        return json.loads(sock.makefile("r", encoding="utf-8").readline())


def metrics(api: str) -> dict:
    try:
        with urllib.request.urlopen(f"{api}/api/v1/audio", timeout=3) as response:
            return json.loads(response.read()).get("metrics", {})
    except Exception:  # noqa: BLE001 - a poll may race a restart
        return {}


def turns(api: str) -> list[dict]:
    try:
        with urllib.request.urlopen(f"{api}/api/v1/turns?limit=5", timeout=3) as response:
            return json.loads(response.read()).get("turns", [])
    except Exception:  # noqa: BLE001
        return []


def wait_for(predicate, timeout: float, interval: float = 0.15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8091")
    parser.add_argument("--inject-port", type=int, default=8871)
    parser.add_argument("--wake", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()

    before = {t.get("ts") for t in turns(args.api)}
    wake_count = metrics(args.api).get("wake_count", 0)

    print(f"1) wake word: {args.wake}")
    print("  ", inject(args.inject_port, args.wake))
    woke = wait_for(lambda: metrics(args.api).get("wake_count", 0) > wake_count, 6.0)
    print(f"   wake detected: {woke}")
    if not woke:
        print("   no wake crossing; check the recording at the configured DOT2_RECORD path")
        return 1

    armed = wait_for(lambda: metrics(args.api).get("gate_armed") is True, 5.0)
    print(f"2) gate armed: {armed}")
    if not armed:
        return 1

    print(f"3) command: {args.command}")
    print("  ", inject(args.inject_port, args.command))

    def got_turn() -> bool:
        return any(t.get("ts") not in before and t.get("transcript") for t in turns(args.api))

    if not wait_for(got_turn, args.timeout, interval=0.5):
        print("   no turn recorded (wake heard, command not transcribed?)")
        return 1

    turn = next(t for t in turns(args.api) if t.get("ts") not in before and t.get("transcript"))
    print("4) turn:", json.dumps({k: turn.get(k) for k in
                                  ("ts", "transcript", "reply", "audio_bytes", "error")},
                                 ensure_ascii=False))
    stages = turn.get("stages") or {}
    if stages:
        print("   stages:", {k: v.get("used") for k, v in stages.items()})
    ok = bool(turn.get("transcript")) and bool(turn.get("reply")) and not turn.get("error")
    print("RESULT:", "ok" if ok else "failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
