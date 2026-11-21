"""A worktree freeze over hundreds of paths never builds a git argv past the Windows command-line cap.

4bfba2's review prep froze 646 paths in one `git diff` argv; CreateProcess refused it and git
"exited -1" with no stderr, twice, halting the run unreviewed.
"""
from __future__ import annotations


import coordinator_core.ops.review_freeze_diff as review_freeze_diff
from coordinator_core.ops.review_freeze_diff import _pathspec_chunks
import pytest

_WINDOWS_CMDLINE_CAP = 32767


def test_chunks_cover_every_spec_in_order_and_stay_under_budget() -> None:
    specs = [f":(literal)coordinator_core/ops/dispatch_emit/tests/test_{i:04d}_a_long_name.py" for i in range(700)]
    chunks = _pathspec_chunks(specs)
    assert len(chunks) > 1
    assert [s for c in chunks for s in c] == specs
    assert all(sum(len(s) + 3 for s in c) <= review_freeze_diff._ARGV_CHAR_BUDGET for c in chunks)
