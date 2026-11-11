"""Tests for the repo-scratch sweep leg of coordinator_core.housekeeping.cycle.run.

Negative-spec: does not re-test sweep_repo_scratch's own classification
(ops/tests/test_repo_scratch_sweep.py) -- only that the cycle calls it, reports
its result, and never lets it fail the cycle.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.housekeeping import cycle
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (plans / "2026-01-01-live.md").write_text(
        "---\nstatus: executing\n---\n\nbody\n", encoding="utf-8"
    )
    _git(root, "add", "docs/plans/2026-01-01-live.md")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def test_cycle_deletes_old_scratch_and_keeps_young(repo: Path) -> None:
    scratch = repo / "scratch"
    scratch.mkdir()
    old = scratch / "old.txt"
    young = scratch / "young.txt"
    old.write_text("x", encoding="utf-8")
    young.write_text("y", encoding="utf-8")
    ancient = time.time() - 365 * 86400.0
    os.utime(old, (ancient, ancient))
    result = cycle.run(repo, 5)
    assert result["scratch_sweep_error"] is None
    assert result["scratch_swept"]["deleted"] >= 1
    assert not old.exists()
    assert young.exists()


def test_cycle_without_scratch_reports_empty_sweep(repo: Path) -> None:
    result = cycle.run(repo, 5)
    assert result["scratch_sweep_error"] is None
    assert result["scratch_swept"]["deleted"] == 0


def test_raising_sweep_is_reported_not_fatal(repo: Path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(cycle, "sweep_repo_scratch", boom)
    result = cycle.run(repo, 5)
    assert "sweep exploded" in result["scratch_sweep_error"]
    assert result["scratch_swept"] is None
    assert result["emitted_prune_error"] is None
