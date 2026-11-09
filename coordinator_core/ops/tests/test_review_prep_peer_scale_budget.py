"""review.prep at 20 peer runs and 150 dirty paths: under the 500ms process bar, spawn count independent of scale."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import review_prep_peer_scale as bench

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SMALL = (5, 30)
LARGE = (20, 150)


def test_fixture_has_peer_commits_halted_records_and_deep_history(tmp_path):
    from coordinator_core.benchmarks import review_prep_dirty_worktree as dw
    from coordinator_core.git.run import run_git

    repo = tmp_path / "repo"
    repo.mkdir()
    dw._git(repo, "init", "-q", "-b", "main")
    (repo / "seed.txt").write_text("s\n", encoding="utf-8")
    dw._git(repo, "add", "-A")
    dw._git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "init")
    params = bench.seed_peer_scale(repo, 20, 150)
    log = run_git(["-C", str(repo), "log", "--format=%(trailers:key=Session-Id,valueonly,separator=%x1d)",
                   f"{params['base']}..HEAD"]).stdout.splitlines()
    sessions = {line.strip() for line in log if line.strip()}
    assert len(log) >= bench.MIN_HISTORY + 20
    assert sessions == {f"peer-sess-{i}" for i in range(20)}
    assert len(list((repo / "state" / "warp").glob("warp-peer-*/halted.json"))) == 20


def test_review_prep_stays_under_the_bar_with_a_spawn_count_independent_of_scale():
    small = bench.measure(*SMALL, tree=False)
    runs = [bench.measure(*LARGE, tree=True) for _ in range(bench.RUNS)]
    slowest = max(runs, key=lambda r: r["process_ms"])
    assert slowest["process_ms"] < bench.PROCESS_BAR_MS, ([r["process_ms"] for r in runs], slowest["legs"])
    for large in runs:
        assert large["spawn_count"] == small["spawn_count"], (small["git_verbs"], large["git_verbs"])
