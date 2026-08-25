"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_review_trail_staged

The review-trail records `ceremony.wsc_tail` writes must reach the ceremony
commit's explicit pathspec.

The defect this closes (observed 2026-08-21, six records for session
`d8f75317`): `review_trail.write` runs inside `_run_precommit_tail`, AFTER the
caller has already assembled `stage_paths`, and nothing folded the resulting
record paths back in. The records landed in the worktree untracked, the
ceremony commit could not carry them, and an explicit follow-up commit naming
them was refused by git with

    pathspec '...' did not match any file(s) known to git

which is git's message for an UNTRACKED path, not a missing one -- the reason
the original report concluded the records had been swept off disk by a reaper.
Measured at the time of the fix: 51 such untracked review-trail records had
accumulated across five sessions and three days, with no reaper involved.

Coverage:
  (a) `_review_trail_stage_paths` recovers worktree-relative paths from a
      tail-op result's `acted` entries, including on Windows, where the
      absolute `out_path` carries its own drive-letter colon.
  (b) a record written outside the worktree (the `REVIEW_TRAIL_OUTPUT_ROOT`
      test-isolation route) is skipped rather than staged.
  (c) a skipped / failed / metadata-incomplete write contributes nothing and
      never raises.

Spec backlink: coordinator_core/ops/ceremony/wsc_tail.py ::
_review_trail_stage_paths
"""

from __future__ import annotations

import os
from pathlib import Path

import coordinator_core.ops.ceremony.tail_ops as tail_ops
import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod


def _acted(*out_paths: Path) -> dict:
    return {
        "acted": [f"{tail_ops.OP_REVIEW_TRAIL}:{p}" for p in out_paths],
        "skipped": [],
        "failed": [],
        "metadata_supplied": True,
    }


def test_written_records_become_worktree_relative_stage_paths(tmp_path: Path):
    root = tmp_path / "repo"
    trail = root / "state" / "review-trail"
    trail.mkdir(parents=True)
    first = trail / "2026-08-21-132927-d8f75317.json"
    second = trail / "2026-08-21-132934-d8f75317.json"
    for p in (first, second):
        p.write_text("{}", encoding="utf-8")

    staged = wsc_tail_mod._review_trail_stage_paths(_acted(first, second), root)

    assert staged == [
        "state/review-trail/2026-08-21-132927-d8f75317.json",
        "state/review-trail/2026-08-21-132934-d8f75317.json",
    ], staged
    assert all(os.sep not in p or os.sep == "/" for p in staged)


def test_record_outside_the_worktree_is_skipped_not_guessed(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "state" / "review-trail").mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "2026-08-21-140515-a2421d8d.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}", encoding="utf-8")

    assert wsc_tail_mod._review_trail_stage_paths(_acted(outside), root) == []


def test_non_acted_results_contribute_nothing(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()

    skipped = {
        "acted": [],
        "skipped": [f"{tail_ops.OP_REVIEW_TRAIL}:no-review-metadata"],
        "failed": [],
        "metadata_supplied": False,
    }
    critical = {
        "acted": [],
        "skipped": [],
        "failed": [],
        "failed_critical": [f"{tail_ops.OP_REVIEW_TRAIL}: b_adjudication present but ..."],
        "metadata_supplied": False,
    }

    assert wsc_tail_mod._review_trail_stage_paths(skipped, root) == []
    assert wsc_tail_mod._review_trail_stage_paths(critical, root) == []
    assert wsc_tail_mod._review_trail_stage_paths({}, root) == []


def test_foreign_acted_entries_are_ignored(tmp_path: Path):
    """Only this op's own `acted` entries are stage candidates -- a sibling
    tail op's entry sharing the result dict must never be staged as a
    review-trail record."""
    root = tmp_path / "repo"
    root.mkdir()

    assert wsc_tail_mod._review_trail_stage_paths(
        {"acted": ["coverage.gate:ok", f"{tail_ops.OP_REVIEW_TRAIL}:"]}, root
    ) == []
