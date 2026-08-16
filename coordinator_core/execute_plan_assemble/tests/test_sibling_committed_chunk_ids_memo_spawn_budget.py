"""coordinator_core/execute_plan_assemble/tests/test_sibling_committed_chunk_ids_memo_spawn_budget.py

Spawn-count regression for `close_out_and_stamp.py::_sibling_committed_chunk_ids`'s
own memoization, following the exact-equality `spawn_count_budget` template
`coordinator_core/benchmarks/budget-manifest.json`'s
`overrides["ceremony.scoped_git_commit"]` entry already carries.

`close_out_and_stamp`'s own main routine calls `_sibling_committed_chunk_ids`
TWICE per closeout with byte-identical inputs -- once inside
`_determine_shipped`, once again purely for `skipped_sibling_repos`. This
test does not reproduce that whole call graph; it isolates the property
that fix depends on: a second call with the SAME inputs as a prior call
must spawn zero additional git subprocesses.

WHY A SPAWN COUNT, NOT A LATENCY FIGURE: this repo runs 50-70 concurrent LLM
sessions at any given moment (CLAUDE.md's "Load norm" section) -- a wall-clock
assertion would be noise.

Found via: state/audits/2026-08-15-fleet-composed-op-spawn-census.md row 10,
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

_DLV = "dlv-memo-test"


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
    entry = manifest["overrides"]["execute_plan_assemble.sibling_committed_chunk_ids_memo"]
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


def test_second_call_with_identical_inputs_spawns_no_additional_git(tmp_path) -> None:
    """No sibling repos named at all (`scope:` empty) is the cheapest real
    shape to isolate the memo property on: `_plan_sibling_repo_ids` itself
    spawns no git, so ANY spawn observed here can only come from a live
    (non-memoized) re-scan. Clears the module-level memo first so this test
    is independent of test execution order."""
    coas._SIBLING_COMMITTED_MEMO.clear()

    root = tmp_path
    _init_repo(root)
    plan_text = "---\ndeliverable_id: d-1\nscope:\n  - some/local/path.py\n---\n\nbody\n"

    first_result, first_spawns = _count_git_calls(
        lambda: coas._sibling_committed_chunk_ids(plan_text, _DLV, ["C1"], root)
    )
    assert first_result == (set(), [])

    second_result, second_spawns = _count_git_calls(
        lambda: coas._sibling_committed_chunk_ids(plan_text, _DLV, ["C1"], root)
    )
    assert second_result == (set(), [])

    budgeted = _manifest_spawn_budget()["second_call_identical_inputs"]
    assert second_spawns == budgeted, (
        "_sibling_committed_chunk_ids second-call spawn count is %d, manifest "
        "budgets %d -- update budget-manifest.json's execute_plan_assemble."
        "sibling_committed_chunk_ids_memo.spawn_count_budget."
        "second_call_identical_inputs (and its _rationale) together with "
        "this test if the change is intentional" % (second_spawns, budgeted)
    )
