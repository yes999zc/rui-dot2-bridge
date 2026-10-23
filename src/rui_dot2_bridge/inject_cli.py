"""Send one WAV (or a tone) to the adapter's test injection port.

    python -m rui_dot2_bridge.inject_cli --wav /tmp/wake.wav
    python -m rui_dot2_bridge.inject_cli --tone 440 --seconds 1.0
"""

from __future__ import annotations

import argparse
import json
import socket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8871)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--wav")
    group.add_argument("--tone", type=float)
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--amplitude", type=float, default=0.9)
    args = parser.parse_args(argv)

    request: dict[str, object] = {}
    if args.wav:
        request["wav"] = args.wav
    else:
        request = {"tone": args.tone, "seconds": args.seconds, "amplitude": args.amplitude}

    with socket.create_connection(("127.0.0.1", args.port), timeout=5.0) as sock:
        sock.sendall((json.dumps(request) + "\n").encode())
        reply = sock.makefile("r", encoding="utf-8").readline().strip()
    print(reply)
    return 0 if '"ok": true' in reply else 1


if __name__ == "__main__":
    raise SystemExit(main())
