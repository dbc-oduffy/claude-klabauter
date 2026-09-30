"""Spawn-count budget for `workday_complete_backfill_scan.main`: the tip-alignment leg costs
O(1) git spawns in the lookback, not O(days).

Counts every `git` process creation during `main()` by wrapping `subprocess.run`; the wrapper is
installed on the shared `subprocess` module, so it sees both the scan module's `_run_git` and
`changelog_ops._batch_resolve_commits`. Spawn count, never wall clock: it is deterministic under load.
"""
from __future__ import annotations

import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core.ops.test_workday_complete_backfill_scan import _commit_on, _git, _make_repo
from coordinator_core.ops.workday_complete_backfill_scan import main
from coordinator_core.session import record_homes

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

TODAY = "2026-03-15"
DAYS = 14

# Git spawns of an all-covered 14-day scan at HEAD, before the tip leg existed (existence-only
# coverage). Measured by running this module at that HEAD and reading the printed count:
#   python -m pytest coordinator_core/ops/test_workday_complete_backfill_scan_spawn_budget.py -s
# (prints "git spawns: lookback 3 = 1, lookback 14 = 1").
HEAD_ALL_COVERED_SPAWNS = 1
TIP_LEG_EXTRA_SPAWNS = 4


def _covered_repo(tmp_path_factory) -> Path:
    repo = _make_repo(tmp_path_factory)
    _changelog_dir = Path(record_homes.home_dir(str(repo), "week-changelog"))
    _changelog_dir.mkdir(parents=True)
    start = date.fromisoformat(TODAY)
    for i in range(1, DAYS + 1):
        day = (start - timedelta(days=i)).isoformat()
        sha = _commit_on(repo, day, f"work {day}")
        (repo / "archive" / "daily-summaries" / f"{day}.md").write_text(f"summary\ncovered_tip_sha: {sha}\n")
        (_changelog_dir / f"{day}.md").write_text("changelog\n")
    return repo


def _count_git_spawns(repo: Path, monkeypatch, lookback: int) -> int:
    monkeypatch.setenv("COORDINATOR_ROOT", str(repo))
    monkeypatch.setenv("COORDINATOR_ROOT_WARN_SUPPRESS", "1")
    calls = {"n": 0}
    orig = subprocess.run

    def _counting_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            calls["n"] += 1
        return orig(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    try:
        rc = main(["--lookback", str(lookback), "--today", TODAY])
    finally:
        monkeypatch.setattr(subprocess, "run", orig)
    assert rc == 0
    return calls["n"]


def test_tip_leg_spawn_count_is_independent_of_lookback(tmp_path_factory, monkeypatch, capsys):
    repo = _covered_repo(tmp_path_factory)
    short = _count_git_spawns(repo, monkeypatch, 3)
    long = _count_git_spawns(repo, monkeypatch, DAYS)
    out = capsys.readouterr().out
    with capsys.disabled():
        print(f"git spawns: lookback 3 = {short}, lookback {DAYS} = {long}")
    assert out.count("\t") == 0, "all-covered fixture must emit no rows"
    assert long == short, f"spawns grew with lookback: 3 -> {short}, {DAYS} -> {long}"
    assert long <= HEAD_ALL_COVERED_SPAWNS + TIP_LEG_EXTRA_SPAWNS, (
        f"{long} spawns exceeds HEAD all-covered {HEAD_ALL_COVERED_SPAWNS} + {TIP_LEG_EXTRA_SPAWNS}"
    )
