"""
coordinator_core.review_trail.tests.test_reviewed_set_spawn_budget

AC5 (binding per K-001): "any rebuild must enter with a spawn budget and a
high-water ratchet from its first commit, never retrofitted." This file is
that budget for `coordinator_core.review_trail.reviewed_set`, following the
worked pattern at
`coordinator_core/tests/test_ipc_per_request_state.py::test_op_timeout_overrides_never_ratchet_upward`
and `coordinator_core/benchmarks/tests/test_spawn_count_budget_ratchet.py`:
a table in THIS file, not the manifest, so raising a bound is a visible
one-line diff, never a silent drift.

Two axes measured, matching the write-time-vs-read-time split the chunk
brief's numbers table turns on:
  * `read_reviewed_set` — MUST be zero spawns, always (AC1). This is a
    hard pin, not a ratchet: any nonzero count here is a regression to
    the read-time-fold-in shape the brief REFUSED on its numbers.
  * `fold_in`, one resolvable single-commit record — high-water marked at
    the measured count. May be LOWERED freely; raising it requires
    editing `_SPAWN_HIGH_WATER` below, turning a silent constant bump
    into a visible diff (same discipline the timeout/spawn-count ratchet
    tables already enforce elsewhere in this repo).

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C1
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.review_trail import reviewed_set as rs

#: High-water mark for `fold_in`'s subprocess count on a single resolvable
#: record: one `git rev-list --all --parents` (reach-set build) + one
#: `git rev-parse --verify` PER DISTINCT endpoint token (2 for a two-
#: endpoint range) + one `git rev-list <range>` (range materialization)
#: = 4. May be lowered freely; raising it requires editing this constant.
_SPAWN_HIGH_WATER = {
    "fold_in_single_record": 4,
}


class _SpawnCounter:
    def __init__(self, real_run):
        self._real_run = real_run
        self.count = 0

    def __call__(self, cmd, *args, **kwargs):
        self.count += 1
        return self._real_run(cmd, *args, **kwargs)


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, encoding="utf-8", check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _make_commit(repo: Path, message: str) -> str:
    _git(["commit", "--allow-empty", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


def test_read_reviewed_set_spawns_zero_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard pin, not a ratchet (AC1): the read path must NEVER spawn."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    rs._append_shas(str(repo), {"a" * 40})
    rs.read_reviewed_set(str(repo))  # warm the resident cache

    counter = _SpawnCounter(subprocess.run)
    monkeypatch.setattr(subprocess, "run", counter)
    rs.read_reviewed_set(str(repo))
    assert counter.count == 0, "read_reviewed_set must spawn zero processes (AC1)"


def test_fold_in_single_record_spawn_budget_ratchet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    sha = _make_commit(repo, "C1")

    counter = _SpawnCounter(subprocess.run)
    monkeypatch.setattr(subprocess, "run", counter)

    result = rs.fold_in(str(repo), [("rec-1", f"{sha}^..{sha}")])
    assert result.folded_record_ids == ["rec-1"]

    high_water = _SPAWN_HIGH_WATER["fold_in_single_record"]
    assert counter.count <= high_water, (
        f"fold_in spawned {counter.count} processes for one record, exceeding "
        f"the high-water mark of {high_water} — if this is an intentional "
        "cost increase, raise _SPAWN_HIGH_WATER in this file (never silently)."
    )


def test_fold_in_spawn_count_flat_across_range_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write-time measurement's central claim: spawn count for `fold_in`
    is FLAT in commit-range size, not per-commit. One record spanning many
    commits must cost the same spawn count as one record spanning one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _make_commit(repo, "C0")
    tip = base
    for i in range(1, 21):
        tip = _make_commit(repo, f"C{i}")

    counter = _SpawnCounter(subprocess.run)
    monkeypatch.setattr(subprocess, "run", counter)
    result = rs.fold_in(str(repo), [("rec-wide", f"{base}..{tip}")])
    assert result.folded_record_ids == ["rec-wide"]
    wide_count = counter.count

    high_water = _SPAWN_HIGH_WATER["fold_in_single_record"]
    assert wide_count <= high_water, (
        f"fold_in over a 20-commit range spawned {wide_count} processes, "
        f"exceeding the flat high-water mark of {high_water} — spawn count "
        "must not scale with range size."
    )
