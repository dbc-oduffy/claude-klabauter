"""The dry-run's destination check refuses a clone behind its upstream or dirty, and writes nothing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from percolate.dest_refresh import RefreshResult, dry_run_refusal, fetch_dest_upstream  # noqa: E402


_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


@pytest.mark.parametrize("flag", [{"pending_abort": True}, {"subsumed": ("peer",)}])
def test_pending_merge_with_zero_behind_is_refused_without_a_behind_count(tmp_path, flag):
    result = RefreshResult(
        repo_root=tmp_path, ok=True, branch="candidate", upstream="origin/candidate", behind=0, **flag
    )
    reason = dry_run_refusal(result)
    assert reason is not None and "merge step" in reason
    assert "0 commit(s) behind" not in reason and "--ff-only" not in reason
