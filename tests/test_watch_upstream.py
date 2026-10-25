"""Pure-logic tests for the upstream watcher's change detection.

The scan itself talks to GitHub; diff() is the part that decides whether a
scan deserves the Lead's attention, so it is tested without the network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watch_upstream  # noqa: E402


def snapshot(**overrides) -> dict:
    base = {
        "scanned_at": "2026-10-25T02:00:00+00:00",
        "tags": ["v0.3.4"],
        "images": ["0.3.4"],
        "publish_runs": [{"name": "docker-publish", "branch": "main",
                          "status": "completed", "conclusion": "success",
                          "created_at": "2026-10-25T01:00:00Z"}],
        "issues": {"3": {"state": "open", "updated_at": "x", "comments": 3,
                         "last_comment_id": 10, "last_author": "yes999zc",
                         "last_at": "2026-10-25T00:00:00Z"}},
        "own": {"issues": [], "prs": []},
        "rui_prs": [{"number": 10, "state": "MERGED", "title": "ci",
                     "author": "yes999zc", "updated_at": "x"}],
        "mentions": [],
    }
    base.update(overrides)
    return base


class DiffTest(unittest.TestCase):
    def test_identical_snapshots_report_nothing(self) -> None:
        self.assertEqual(watch_upstream.diff(snapshot(), snapshot()), [])

    def test_new_tag_is_reported(self) -> None:
        changes = watch_upstream.diff(snapshot(), snapshot(tags=["v0.3.4", "v0.4.0"]))
        self.assertTrue(any("NEW rui tag(s)" in line and "v0.4.0" in line for line in changes))

    def test_new_image_is_reported(self) -> None:
        changes = watch_upstream.diff(snapshot(), snapshot(images=["0.3.4", "0.4.0"]))
        self.assertTrue(any("NEW rui image(s)" in line and "0.4.0" in line for line in changes))

    def test_tag_without_image_is_flagged(self) -> None:
        changes = watch_upstream.diff(
            snapshot(), snapshot(tags=["v0.3.4", "v0.4.0"], images=["0.3.4"])
        )
        self.assertTrue(any("still missing from ghcr" in line for line in changes))

    def test_new_issue_comment_is_reported(self) -> None:
        changed = snapshot()
        changed["issues"] = {"3": {**snapshot()["issues"]["3"],
                                   "comments": 4, "last_comment_id": 11}}
        changes = watch_upstream.diff(snapshot(), changed)
        self.assertTrue(any("rui#3: new comment" in line for line in changes))

    def test_new_own_pr_is_reported(self) -> None:
        changed = snapshot()
        changed["own"] = {"issues": [], "prs": [
            {"number": 1, "state": "open", "title": "fix adapter", "author": "someone"},
        ]}
        changes = watch_upstream.diff(snapshot(), changed)
        self.assertTrue(any("own pr #1" in line and "someone" in line for line in changes))

    def test_new_rui_pr_is_reported(self) -> None:
        changed = snapshot()
        changed["rui_prs"] = snapshot()["rui_prs"] + [
            {"number": 11, "state": "OPEN", "title": "plug docs", "author": "yes999zc",
             "updated_at": "y"},
        ]
        changes = watch_upstream.diff(snapshot(), changed)
        self.assertTrue(any("rui PR #11" in line for line in changes))

    def test_publish_failure_is_reported(self) -> None:
        changed = snapshot()
        changed["publish_runs"] = [{"name": "docker-publish", "branch": "main",
                                    "status": "completed", "conclusion": "failure",
                                    "created_at": "2026-10-25T03:00:00Z"}]
        changes = watch_upstream.diff(snapshot(), changed)
        self.assertTrue(any("publish/CI" in line for line in changes))

    def test_semver_sort_ignores_sha_tags(self) -> None:
        ordered = sorted(["sha-1d0", "0.3.4", "0.4.0", "latest"],
                         key=watch_upstream.semver_key)
        self.assertEqual(ordered[-1], "0.4.0")
        # Non-version tags sort below every semver tag; they are not releases.
        self.assertEqual(watch_upstream.semver_key("sha-1d0"), (-1,))
        self.assertEqual(watch_upstream.semver_key("latest"), (-1,))


if __name__ == "__main__":
    unittest.main()
