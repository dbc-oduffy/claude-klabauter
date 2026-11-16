"""review.prep at 20 peer runs and 150 dirty paths: median-of-5 process time under the 200ms bar, slowest under the 500ms kill bar, spawn count independent of scale."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import review_prep_peer_scale as bench

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SMALL = (5, 30)
LARGE = (20, 150)
SPAWN_CEILING = 2  # one `git log` (peer bases) and one `git diff` (whole freeze)


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


def _seeded_peer_repo(tmp_path):
    from coordinator_core.benchmarks import review_prep_dirty_worktree as dw

    repo = tmp_path / "repo"
    repo.mkdir()
    dw._git(repo, "init", "-q", "-b", "main")
    (repo / "seed.txt").write_text("s\n", encoding="utf-8")
    dw._git(repo, "add", "-A")
    dw._git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "init")
    return repo, bench.seed_peer_scale(repo, 6, 40)


def _stanzas(text):
    return sorted(s for s in text.split("diff --git ") if s)


def _freeze(review_prep, repo, params, prefix):
    from coordinator_core.benchmarks import review_prep_dirty_worktree as dw
    import json

    declared = json.loads((repo / "state" / "warp" / dw.RUN_ID / "manifest.json").read_text(encoding="utf-8"))[
        "review_declared_paths"]
    peer = review_prep._peer_bases(repo, params["base"], None, declared, "own-sess", params["plan_path"])
    assert peer, "fixture must key paths to peer bases"
    got = review_prep._freeze_whole_by_base(repo, params["base"], None, declared, peer, prefix)
    assert got["error"] is None
    from pathlib import Path

    return Path(got["diff_path"]).read_text(encoding="utf-8")


def test_worktree_freeze_over_peer_bases_is_one_diff_with_the_per_base_content(tmp_path, monkeypatch):
    from coordinator_core.ops import review_prep

    repo, params = _seeded_peer_repo(tmp_path)
    calls = []
    real = review_prep._heavy("freeze_diff")
    monkeypatch.setattr(review_prep, "freeze_diff", lambda *a, **k: calls.append(a[2]) or real(*a, **k), raising=False)
    merged = _freeze(review_prep, repo, params, "t-merged")
    assert calls == ["t-merged-g1-prep"]
    calls.clear()
    monkeypatch.setattr("coordinator_core.git.ref_cas_retry.paths_unchanged_between", lambda *a, **k: False)
    sparse = _freeze(review_prep, repo, params, "t-sparse")  # no single existing base fits: the sparse tree
    assert calls == ["t-sparse-g1-prep"]
    calls.clear()
    monkeypatch.setattr(review_prep, "_merged_base", lambda *a, **k: None)
    split = _freeze(review_prep, repo, params, "t-split")
    assert len(calls) >= 2
    assert _stanzas(merged) == _stanzas(sparse) == _stanzas(split) and merged


def test_review_prep_stays_under_the_bar_with_a_spawn_count_independent_of_scale():
    small = bench.measure(*SMALL, tree=False)
    runs = [bench.measure(*LARGE, tree=True) for _ in range(bench.RUNS)]
    slowest = max(runs, key=lambda r: r["process_ms"])
    per_run = [r["process_ms"] for r in runs]
    assert slowest["process_ms"] < bench.PROCESS_BAR_MS, (per_run, slowest["legs"])
    median = sorted(per_run)[len(per_run) // 2]
    assert len(runs) == bench.RUNS == 5
    assert median < bench.REVIEW_PREP_BAR_MS, (median, per_run, slowest["legs"])
    for large in runs:
        assert large["spawn_count"] == small["spawn_count"], (small["git_verbs"], large["git_verbs"])
        assert large["spawn_count"] <= SPAWN_CEILING, large["git_verbs"]
