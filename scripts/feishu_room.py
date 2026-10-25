#!/usr/bin/env python3
"""Talk in the shared Feishu room (Phase 0 of the agent-to-agent channel).

Two agents (dot2 bridge side and rui maintainer side) post into one group chat;
the human watches the same room, can interject and can stop the exchange. The
protocol lives in docs/feishu-room.md; this file is only the transport.

    feishu_room.py say --as dot2 --type INFO --text "0.4.1 已发布"
    feishu_room.py tail --limit 10
    feishu_room.py wait --for rui --timeout 300     # block until the other side speaks

Both sides run under the same bot identity today, so the [dot2]/[rui] prefix is
the only way to tell who is speaking: say() always writes it.

Exit codes: 0 ok, 1 transport/usage error, 2 wait timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

CHAT_ID = os.environ.get("FEISHU_ROOM_CHAT_ID", "oc_2c6b8364d0b180c9a79722461ad92b2f")
LARK = os.environ.get("LARK_CLI", "/opt/homebrew/bin/lark-cli")
ENV = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
       "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}
TYPES = ("Q", "A", "DECISION", "INFO", "BLOCKER", "HUMAN")
MAX_TEXT = 8000


def _run(args: list[str]) -> dict:
    proc = subprocess.run([LARK, *args], capture_output=True, text=True, env=ENV, timeout=60)
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError((proc.stderr or "lark-cli returned nothing").strip()[:300])
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli returned non-JSON: {out[:200]}") from exc
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise RuntimeError(f"{error.get('type')}/{error.get('subtype')}: {error.get('message')}")
    return payload.get("data") or {}


def messages(limit: int = 50) -> list[dict]:
    data = _run(["im", "+chat-messages-list", "--as", "bot", "--chat-id", CHAT_ID,
                 "--page-all", "--format", "json"])
    items = data.get("messages") or data.get("items") or []
    items = [m for m in items if not m.get("deleted")]
    return items[:limit] if limit else items


def _line(message: dict, width: int = 400) -> str:
    who = (message.get("sender") or {}).get("name") or "?"
    content = (message.get("content") or "").strip().replace("\n", " ⏎ ")
    if len(content) > width:
        content = content[:width] + "…"
    return f"[{message.get('create_time', '?')}] {who}: {content}"


def cmd_say(args: argparse.Namespace) -> int:
    tag = args.kind.upper()
    if tag not in TYPES:
        print(f"unknown --type {tag}; use one of {', '.join(TYPES)}", file=sys.stderr)
        return 1
    body = f"[{args.alias}][{tag}] {args.text.strip()}"
    if len(body) > MAX_TEXT:
        print(f"message too long ({len(body)} > {MAX_TEXT})", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    try:
        data = _run(["im", "+messages-send", "--as", "bot", "--chat-id", CHAT_ID,
                     "--text", f"@{path}"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    print(f"sent {data.get('message_id', '?')} as [{args.alias}][{tag}]")
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    items = messages(args.limit)
    for message in reversed(items):  # oldest first
        print(_line(message))
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    pattern = re.compile(args.match) if args.match else None
    alias = re.compile(rf"\[{re.escape(args.watch) if args.watch else 'rui'}\]")
    seen = {m.get("message_id") for m in messages(50)}
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        for message in reversed(messages(50)):
            if message.get("message_id") in seen:
                continue
            seen.add(message.get("message_id"))
            content = message.get("content") or ""
            if alias.search(content) and (pattern is None or pattern.search(content)):
                print(_line(message, width=2000))
                return 0
    print(f"no matching message within {args.timeout}s", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="post one message")
    say.add_argument("--as", dest="alias", default="dot2", choices=("dot2", "rui", "human"))
    say.add_argument("--type", dest="kind", default="INFO")
    say.add_argument("--text", required=True)
    say.set_defaults(func=cmd_say)

    tail = sub.add_parser("tail", help="print the last messages, oldest first")
    tail.add_argument("--limit", type=int, default=20)
    tail.set_defaults(func=cmd_tail)

    wait = sub.add_parser("wait", help="block until the other side speaks")
    wait.add_argument("--watch", default="rui", choices=("dot2", "rui", "human"))
    wait.add_argument("--match", default="", help="regex the content must also match")
    wait.add_argument("--interval", type=int, default=20)
    wait.add_argument("--timeout", type=int, default=300)
    wait.set_defaults(func=cmd_wait)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"feishu_room: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
