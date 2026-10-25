#!/usr/bin/env python3
"""Watch the upstream rui project (and our own) for things that need a human.

The Lead does not want to poll GitHub by hand while another developer iterates
on rui. This script is the deterministic half of that watch: it collects the
facts (tags, published images, CI/publish status, issue comments, PRs in both
repositories) and prints only what changed since the last run. The agent half
is the rui-watch teammate, which reads this digest and decides what to answer,
log or escalate.

Usage:
    python3 scripts/watch_upstream.py                 # digest, no state write
    python3 scripts/watch_upstream.py --update-state  # digest + remember
    python3 scripts/watch_upstream.py --json          # machine readable

Exit codes: 0 = scanned (change or not), 1 = the scan itself failed.

No secrets are read or printed: only public repository metadata, via the
already-authenticated gh CLI.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

RUI_REPO = "yes999zc/rui"
OWN_REPO = "yes999zc/rui-dot2-bridge"
IMAGE_PACKAGE = "rui"
WATCHED_ISSUES = (3, 7, 1)
STATE_PATH = Path(__file__).resolve().parent.parent / ".watch" / "rui-state.json"
GH_TIMEOUT_S = 30


def gh_bin() -> str:
    for candidate in (shutil.which("gh"), "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("gh CLI not found; install it or put it on PATH")


def gh_json(args: list[str]) -> object:
    """Run one gh api call and parse JSON. Raises RuntimeError with a short reason."""
    proc = subprocess.run(
        [gh_bin(), *args],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise RuntimeError(detail[0][:200] if detail else f"gh exited {proc.returncode}")
    text = proc.stdout.strip()
    return json.loads(text) if text else None


def semver_key(tag: str) -> tuple:
    """Sort key for vX.Y.Z / X.Y.Z tags; anything else sorts lowest."""
    cleaned = tag.lstrip("v")
    parts = cleaned.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return (-1,)


def scan_tags() -> list[str]:
    tags = gh_json(["api", f"repos/{RUI_REPO}/tags?per_page=100"]) or []
    return [item["name"] for item in tags]


def scan_images() -> list[str]:
    versions = gh_json(
        ["api", f"/user/packages/container/{IMAGE_PACKAGE}/versions?per_page=100"]
    ) or []
    images = []
    for version in versions:
        images.extend(version.get("metadata", {}).get("container", {}).get("tags", []))
    return sorted(images)


def scan_publish_runs() -> list[dict]:
    runs = gh_json(["api", f"repos/{RUI_REPO}/actions/runs?per_page=10"]) or {}
    picked = []
    for run in runs.get("workflow_runs", []):
        if run.get("name", "").lower() in ("docker-publish", "release", "ci"):
            picked.append({
                "name": run.get("name"),
                "branch": run.get("head_branch"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "created_at": run.get("created_at"),
            })
    return picked


def scan_issue(number: int) -> dict:
    issue = gh_json([
        "api", f"repos/{RUI_REPO}/issues/{number}",
    ]) or {}
    comments = gh_json([
        "api", f"repos/{RUI_REPO}/issues/{number}/comments?per_page=100",
    ]) or []
    last = comments[-1] if comments else {}
    return {
        "state": issue.get("state"),
        "updated_at": issue.get("updated_at"),
        "comments": len(comments),
        "last_comment_id": last.get("id"),
        "last_author": (last.get("user") or {}).get("login"),
        "last_at": last.get("created_at"),
    }


def scan_own_repo() -> dict:
    issues = gh_json(["api", f"repos/{OWN_REPO}/issues?state=all&per_page=10"]) or []
    prs = gh_json(["api", f"repos/{OWN_REPO}/pulls?state=all&per_page=10"]) or []
    return {
        "issues": [
            {"number": i.get("number"), "state": i.get("state"),
             "title": (i.get("title") or "")[:120],
             "author": (i.get("user") or {}).get("login")}
            for i in issues
        ],
        "prs": [
            {"number": p.get("number"), "state": p.get("state"),
             "title": (p.get("title") or "")[:120],
             "author": (p.get("user") or {}).get("login")}
            for p in prs
        ],
    }


def scan_rui_prs() -> list[dict]:
    prs = gh_json(["api", f"repos/{RUI_REPO}/pulls?state=all&per_page=10"]) or []
    return [
        {"number": p.get("number"), "state": p.get("state"),
         "title": (p.get("title") or "")[:120],
         "author": (p.get("user") or {}).get("login"),
         "updated_at": p.get("updated_at")}
        for p in prs
    ]


def scan_mentions() -> list[dict]:
    """Issues/PRs in rui that mention dot2/bridge in title or body."""
    result = gh_json([
        "api", f"search/issues?q=repo:{RUI_REPO}+dot2&per_page=10",
    ]) or {}
    return [
        {"number": item.get("number"), "state": item.get("state"),
         "title": (item.get("title") or "")[:120]}
        for item in result.get("items", [])
    ]


def collect() -> dict:
    return {
        "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tags": scan_tags(),
        "images": scan_images(),
        "publish_runs": scan_publish_runs(),
        "issues": {str(n): scan_issue(n) for n in WATCHED_ISSUES},
        "own": scan_own_repo(),
        "rui_prs": scan_rui_prs(),
        "mentions": scan_mentions(),
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(snapshot: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


def diff(old: dict, new: dict) -> list[str]:
    """Human lines describing only what is new. Empty means 'nothing changed'."""
    changes: list[str] = []

    def semver(values: list[str]) -> list[str]:
        return sorted(
            (v for v in values if semver_key(v) != (-1,)),
            key=semver_key,
        )

    old_tags, new_tags = semver(old.get("tags", [])), semver(new.get("tags", []))
    added_tags = [t for t in new_tags if t not in old_tags]
    if added_tags:
        changes.append("NEW rui tag(s): " + ", ".join(added_tags))

    old_images, new_images = semver(old.get("images", [])), semver(new.get("images", []))
    added_images = [t for t in new_images if t not in old_images]
    if added_images:
        changes.append("NEW rui image(s) in ghcr: " + ", ".join(added_images))
    # A tag may exist while its image never reached ghcr (exactly what blocked us
    # on 0.4.0). Report the mismatch on every scan, not only when an image lands.
    newest_tag = new_tags[-1] if new_tags else ""
    if newest_tag and newest_tag.lstrip("v") not in new_images:
        changes.append(
            f"NOTE image for {newest_tag} is still missing from ghcr "
            "(docker-publish failing?)"
        )

    old_pub = (old.get("publish_runs") or [{}])[0]
    new_pub = (new.get("publish_runs") or [{}])[0]
    if new_pub and (new_pub.get("conclusion"), new_pub.get("created_at")) != (
        old_pub.get("conclusion"), old_pub.get("created_at")
    ):
        changes.append(
            "publish/CI: {} {} {} -> {}".format(
                new_pub.get("name"), new_pub.get("branch"),
                new_pub.get("status"), new_pub.get("conclusion"),
            )
        )

    for number, info in new.get("issues", {}).items():
        before = (old.get("issues") or {}).get(number) or {}
        if before.get("last_comment_id") != info.get("last_comment_id"):
            changes.append(
                "rui#{n}: new comment (#{c} by {a} at {t}); total {n_comments}".format(
                    n=number, c=info.get("comments"), a=info.get("last_author"),
                    t=info.get("last_at"), n_comments=info.get("comments"),
                )
            )
        elif before.get("state") != info.get("state"):
            changes.append(f"rui#{number}: state {before.get('state')} -> {info.get('state')}")

    old_own = old.get("own") or {}
    for kind in ("issues", "prs"):
        before = {item["number"] for item in old_own.get(kind, [])}
        for item in new.get("own", {}).get(kind, []):
            if item["number"] not in before:
                changes.append(
                    "own {kind} #{n} [{state}] {title} by {author}".format(
                        kind=kind[:-1], n=item["number"], state=item["state"],
                        title=item["title"], author=item["author"],
                    )
                )

    old_prs = {item["number"]: item for item in old.get("rui_prs", [])}
    for item in new.get("rui_prs", []):
        before = old_prs.get(item["number"])
        if before is None:
            changes.append(
                "rui PR #{n} [{state}] {title} by {author}".format(
                    n=item["number"], state=item.get("state"),
                    title=item.get("title"), author=item.get("author"),
                )
            )
        elif before.get("state") != item.get("state"):
            changes.append(
                "rui PR #{n}: {a} -> {b}".format(
                    n=item["number"], a=before.get("state"), b=item.get("state")
                )
            )

    old_mentions = {item["number"] for item in old.get("mentions", [])}
    for item in new.get("mentions", []):
        if item["number"] not in old_mentions:
            changes.append(
                "rui #{n} [{state}] mentions dot2: {title}".format(
                    n=item["number"], state=item.get("state"), title=item.get("title"),
                )
            )
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw snapshot")
    parser.add_argument("--update-state", action="store_true", help="remember this snapshot")
    args = parser.parse_args()

    try:
        snapshot = collect()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"watch_upstream: scan failed: {exc}", file=sys.stderr)
        return 1

    old = load_state()
    changes = diff(old, snapshot) if old else ["(first run: no previous state)"]

    if args.json:
        print(json.dumps({"snapshot": snapshot, "changes": changes}, ensure_ascii=False, indent=2))
    else:
        latest_tag = sorted(snapshot["tags"], key=semver_key)[-1] if snapshot["tags"] else "?"
        latest_image = sorted(snapshot["images"], key=semver_key)[-1] if snapshot["images"] else "none"
        print(f"scanned {snapshot['scanned_at']}  rui latest tag={latest_tag}  latest image={latest_image}")
        for line in changes:
            print(" -", line)
        if not changes:
            print(" no changes")

    if args.update_state:
        save_state(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
