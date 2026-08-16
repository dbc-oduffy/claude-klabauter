"""coordinator_core/execute_plan_assemble/tests/test_dispatch_ledger_delivered_spawn_budget.py

Spawn-count regression for `close_out_and_stamp.py::_dispatch_ledger_delivered`
(legacy Dispatch Ledger fallback oracle), following the exact-equality
`spawn_count_budget` template `coordinator_core/benchmarks/budget-manifest.json`'s
`overrides["ceremony.scoped_git_commit"]` entry already carries.

WHY A SPAWN COUNT, NOT A LATENCY FIGURE: this repo runs 50-70 concurrent LLM
sessions at any given moment (CLAUDE.md's "Load norm" section) -- a wall-clock
assertion would be noise. Spawn count is deterministic under load and only
moves when the code changes how many `git` subprocesses it issues per call
shape.

Found via: state/audits/2026-08-15-fleet-composed-op-spawn-census.md row 18,
reverified in state/audits/2026-08-15-fleet-census-reverification-at-head.md
"Rows surviving intact and unguarded".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.benchmarks import budget

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(root: Path) -> None:
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "t@t.example"], root)
    _run_git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("v1\n", encoding="utf-8")
    _run_git(["add", "seed.txt"], root)
    _run_git(["commit", "-q", "-m", "seed"], root)


def _manifest_spawn_budget() -> dict:
    manifest = budget.load_manifest()
    entry = manifest["overrides"]["execute_plan_assemble.dispatch_ledger_delivered"]
    return entry["spawn_count_budget"]


def _count_git_calls(fn):
    calls = {"n": 0}
    orig = subprocess.run

    def _counting_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            calls["n"] += 1
        return orig(cmd, *a, **kw)

    coas.subprocess.run = _counting_run
    try:
        result = fn()
    finally:
        coas.subprocess.run = orig
    return result, calls["n"]


def test_no_committed_rows_spawns_zero_git(tmp_path) -> None:
    """A Dispatch Ledger table whose rows are all non-`committed <sha>`-
    shaped never has a sha to check at all -- must spawn zero git."""
    root = tmp_path
    _init_repo(root)
    plan_text = (
        "## Dispatch Ledger\n\n"
        "| chunk-id | status |\n"
        "|---|---|\n"
        "| C1 | ready -- not yet dispatched |\n"
    )

    (is_shipped, missing, spawns) = (None, None, None)

    def _call():
        return coas._dispatch_ledger_delivered(plan_text, root)

    (is_shipped, missing, error), spawns = _count_git_calls(_call)

    assert error is None
    assert is_shipped is False
    assert missing == ["C1"]

    budgeted = _manifest_spawn_budget()["no_committed_rows"]
    assert spawns == budgeted, (
        "_dispatch_ledger_delivered spawn count is %d, manifest budgets %d "
        "-- update budget-manifest.json's "
        "execute_plan_assemble.dispatch_ledger_delivered.spawn_count_budget."
        "no_committed_rows (and its _rationale) together with this test if "
        "the change is intentional" % (spawns, budgeted)
    )


def test_multiple_committed_rows_spawn_exactly_two_git_calls(tmp_path) -> None:
    """Several rows each citing a `committed <sha>` status must still cost
    exactly the manifest's `spawn_count_budget.n_committed_rows` subprocess
    invocations -- ONE batched `git cat-file --batch-check` plus ONE
    `git rev-list HEAD`, not two spawns per row (pre-fix: 2N spawns for N
    rows)."""
    root = tmp_path
    _init_repo(root)

    (root / "f1.txt").write_text("a\n", encoding="utf-8")
    _run_git(["add", "f1.txt"], root)
    _run_git(["commit", "-q", "-m", "work 1"], root)
    sha1 = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()

    (root / "f2.txt").write_text("b\n", encoding="utf-8")
    _run_git(["add", "f2.txt"], root)
    _run_git(["commit", "-q", "-m", "work 2"], root)
    sha2 = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()

    plan_text = (
        "## Dispatch Ledger\n\n"
        "| chunk-id | status |\n"
        "|---|---|\n"
        f"| C1 | committed {sha1} |\n"
        f"| C2 | committed {sha2} (EM-inline) |\n"
        "| C3 | committed 0000000000000000000000000000000000000000 |\n"
    )

    def _call():
        return coas._dispatch_ledger_delivered(plan_text, root)

    (is_shipped, missing, error), spawns = _count_git_calls(_call)

    assert error is None
    assert is_shipped is False
    assert missing == ["C3"]

    budgeted = _manifest_spawn_budget()["n_committed_rows"]
    assert spawns == budgeted, (
        "_dispatch_ledger_delivered spawn count is %d, manifest budgets %d "
        "-- update budget-manifest.json's "
        "execute_plan_assemble.dispatch_ledger_delivered.spawn_count_budget."
        "n_committed_rows (and its _rationale) together with this test if "
        "the change is intentional" % (spawns, budgeted)
    )
