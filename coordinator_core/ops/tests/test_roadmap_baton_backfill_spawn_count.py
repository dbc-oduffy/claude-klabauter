"""`roadmap.baton_backfill` proof legs spawn a fixed number of git processes, whatever the baton count.

One prefix resolution, one ancestry call and one reachability check cover every met_by sha and every
proposed shipped_in; the selection itself reads the corpus in process.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import roadmap_baton_backfill as op
from coordinator_core.ops.tests.test_roadmap_baton_backfill import (  # noqa: F401
    PLAN, _coded, _git, _track, _write_baton, _write_plan, repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PROOF_SPAWNS = 3


def _corpus(repo, count: int) -> None:
    _write_plan(repo, [_coded(repo.first), "open"])
    for i in range(count):
        _write_baton(repo, f"b{i}")
    _track(repo)


@pytest.mark.parametrize("count", [2, 7])
def test_dry_run_proof_spawns_three_git_calls_whatever_the_baton_count(repo, monkeypatch, count):
    _corpus(repo, count)
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = op._handler({"met_by": {PLAN: {"C1": repo.second[:10]}}}, repo_root=repo)
    assert len(out["proposals"]) == count and out["refused"] == {}
    assert len(spawns) == _PROOF_SPAWNS, [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)


def test_a_dry_run_with_no_met_by_spawns_only_the_reachability_check(repo, monkeypatch):
    _write_plan(repo, [_coded(repo.first), _coded(repo.second)])
    for i in range(5):
        _write_baton(repo, f"b{i}")
    _track(repo)
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = op._handler({}, repo_root=repo)
    assert len(out["proposals"]) == 5
    assert len(spawns) == 1 and "rev-list" in spawns[0].argv, [s.argv for s in spawns]
