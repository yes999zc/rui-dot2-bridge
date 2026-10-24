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
    # After a wake the plane holds the gate for cue + ack.echo_ignore_ms (default 1200 ms,
    # cue ~490 ms), then opens a 5 s window. Speaking before the hold ends is discarded;
    # speaking after the window is another idle session. 2.1 s lands inside it.
    parser.add_argument("--settle", type=float, default=2.1)
    parser.add_argument("--attempts", type=int, default=1,
                        help="retry the whole wake+command sequence (self-injection is intermittent)")
    parser.add_argument("--repeat-command", action="store_true",
                        help="send the command a second time 0.7 s later (hold overshoot)")
    args = parser.parse_args()

    before = {t.get("ts") for t in turns(args.api)}

    def new_turn() -> dict | None:
        return next((t for t in turns(args.api)
                     if t.get("ts") not in before and t.get("transcript")), None)

    for attempt in range(1, max(1, args.attempts) + 1):
        print(f"\n=== attempt {attempt}/{args.attempts} ===")
        wake_count = metrics(args.api).get("wake_count", 0)
        print(f"1) wake word: {args.wake}")
        print("  ", inject(args.inject_port, args.wake))
        if not wait_for(lambda count=wake_count: metrics(args.api).get("wake_count", 0) > count, 6.0):
            print("   no wake crossing; retrying")
            time.sleep(2.0)
            continue
        print("   wake detected")

        # No gate polling: the metric named gate_armed belongs to the follow-up gate,
        # not the wake window (that mistake cost a full evening - the command was
        # injected seconds after the wake window had already closed). The wake window
        # starts after cue + ack.echo_ignore_ms (default 490 + 1200 = 1.69 s).
        print(f"2) settling {args.settle:.1f} s so the ack cue's hold ends")
        time.sleep(args.settle)

        print(f"3) command: {args.command}")
        print("  ", inject(args.inject_port, args.command))
        if args.repeat_command:
            time.sleep(0.7)
            print("   repeat:", inject(args.inject_port, args.command))

        if wait_for(lambda: new_turn() is not None, args.timeout, interval=0.5):
            turn = new_turn() or {}
            print("4) turn:", json.dumps({k: turn.get(k) for k in
                                          ("ts", "transcript", "reply", "audio_bytes", "error")},
                                         ensure_ascii=False))
            stages = turn.get("stages") or {}
            if stages:
                print("   stages:", {k: v.get("used") for k, v in stages.items()})
            ok = bool(turn.get("transcript")) and bool(turn.get("reply")) and not turn.get("error")
            print("RESULT:", "ok" if ok else "failed")
            return 0 if ok else 1
        print("   wake heard but no turn; letting the session close before retrying")
        time.sleep(6.0)

    print("RESULT: no turn after all attempts")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
