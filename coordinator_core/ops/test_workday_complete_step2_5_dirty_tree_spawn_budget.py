"""coordinator_core/ops/test_workday_complete_step2_5_dirty_tree_spawn_budget.py

Spawn-count regression for `workday_complete_step2_5_dirty_tree.py::_classify_main_pass`'s
EOL-PHANTOM / SUBMODULE probes, following the exact-equality `spawn_count_budget` convention
every row in `coordinator_core/benchmarks/budget-manifest.json` carries -- an exact-count
ceiling per call shape, not a latency figure (see `ceremony.wsc_tail`'s row for a live worked
example; the row this docstring used to cite, `overrides["ceremony.scoped_git_commit"]`, was
deleted at K-045).

WHY A SPAWN COUNT, NOT A LATENCY FIGURE: this repo runs 50-70 concurrent LLM
sessions at any given moment (CLAUDE.md's "Load norm" section) — a wall-clock
assertion would be noise. Spawn count is deterministic under load and only
moves when the code changes how many `git` subprocesses it issues per call
shape — exactly the regression class the manifest's `spawn_count_budget`
convention exists to catch.

Found via: state/audits/2026-08-15-fleet-census-reverification-at-head.md
§ "Lower confidence, fan-out unit unconfirmed" — verified live, load-bearing
(feeds the EOL-PHANTOM/SUBMODULE/downstream classification branches), then
fixed.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks import budget
from coordinator_core.ops.workday_complete_step2_5_dirty_tree import (
    _Accumulators,
    _Counters,
    _classify_main_pass,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    return repo


def _manifest_spawn_budget() -> dict:
    manifest = budget.load_manifest()
    entry = manifest["overrides"]["bin.workday_complete_step2_5_dirty_tree.classify_main_pass"]
    return entry["spawn_count_budget"]


def test_classify_main_pass_spawns_exactly_two_git_calls_for_several_dirty_paths(tmp_path):
    """Several dirty paths spanning tracked-modified, untracked, and
    committed-then-untouched (EOL-phantom candidate) states must still cost
    exactly the manifest's `spawn_count_budget.per_classify_call` `git`
    subprocess invocations for the EOL-PHANTOM + SUBMODULE probes — two
    batched calls total, not two per dirty path (pre-fix: up to 2N spawns
    for N paths)."""
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    (repo / "b.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    # a.txt: real content change (has diff content).
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    # b.txt: touched but content-identical (EOL-phantom candidate).
    (repo / "b.txt").write_text("v1\n", encoding="utf-8")
    # c.txt: new untracked file.
    (repo / "c.txt").write_text("new\n", encoding="utf-8")

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.splitlines()

    counters = _Counters()
    acc = _Accumulators()

    import coordinator_core.ops.workday_complete_step2_5_dirty_tree as mod

    calls = {"n": 0}
    orig = subprocess.run

    def _counting_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            calls["n"] += 1
        return orig(cmd, *a, **kw)

    mod.subprocess.run = _counting_run
    try:
        _classify_main_pass(status, str(repo), counters, acc)
    finally:
        mod.subprocess.run = orig

    budgeted = _manifest_spawn_budget()["per_classify_call"]
    assert calls["n"] == budgeted, (
        "_classify_main_pass spawn count is %d, manifest budgets %d -- "
        "update budget-manifest.json's "
        "bin.workday_complete_step2_5_dirty_tree.classify_main_pass."
        "spawn_count_budget.per_classify_call (and its _rationale) together "
        "with this test if the change is intentional" % (calls["n"], budgeted)
    )
