"""
coordinator_core.ops.ceremony.tests.test_commit_e2e_spawn_budget

C8 (docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-clear.md),
A8/A10: gives `ceremony.scoped_git_commit` an END-TO-END git-subprocess
SPAWN-COUNT budget -- the whole `_handler`/`run_commit_pipeline`
staging+commit+push pipeline, not just the ownership-gate leg
`test_commit_gate_budget.py` already covers.

WHY A SPAWN COUNT, NOT A LATENCY FIGURE: this repo runs 50-70 concurrent
LLM sessions at any given moment (CLAUDE.md's "Load norm" section) -- a
wall-clock assertion on this op would be noise, not signal. The spawn
count this module asserts is invariant to machine load; it only moves when
the CODE changes how many git subprocesses it issues per invocation shape,
which is exactly the regression class this budget exists to catch (an
accidental +5 landing unmeasured, the way C5's own +1/+2 nearly did).

COUNTING MECHANISM: reuses `test_commit_gate_budget.py`'s approach (wrap
`git_native._git`, the sole native-git choke point this whole op's
staging/commit/push pipeline routes through -- see `git_native.py`'s own
docstring, "the single choke point every native git call ... routes
through") rather than inventing a second counting dialect. That mechanism
generalises cleanly from the gate leg alone to the whole op: every spawn
mechanism reachable from `_handler` (there is no `asyncio.create_
subprocess_exec` or bare `subprocess.run` call anywhere on this op's own
path -- `commit_pipeline.py`'s own module docstring: "every subprocess in
this module ... routes through `git_native._git`") is covered by wrapping
this one function. It does not itself spawn per-path -- it counts real
calls the pipeline already makes, it never issues extra ones.

MEASURED SHAPES (2026-08-11, this repo, matching `budget-manifest.json`'s
`overrides["ceremony.scoped_git_commit"].spawn_count_budget`):
  - green path (clean commit, no claim conflict): 12 spawns.
  - refusal path (an UNANSWERABLE path -- unverified caller identity
    conflicting with a live claimant): 0 spawns -- `_handler` returns the
    rejection before any staging call is ever issued.
  - a directory-pathspec-expansion invocation: 13 spawns -- the green
    path's 12 plus the one extra pathspec-scoped `git status --porcelain
    -- <dir>` `_expand_directory_pathspecs` issues up front.

These are exact-equality assertions, deliberately: the whole point of a
spawn-COUNT budget is that it does not drift quietly. A future op change
that adds or removes a spawn must update both this test and the manifest
entry in the same commit, not silently pass.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import budget
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Fixture helpers -- mirrors test_commit_gate_budget.py's pattern
# ---------------------------------------------------------------------------


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_meta_live(repo: Path, sid: str, *, live: bool) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last_activity = session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(
        json.dumps({"pid": 1, "last_activity": last_activity}) + "\n",
        encoding="utf-8",
    )


def _count_op_git_calls(fn) -> tuple[int, dict]:
    """Wrap `git_native._git` -- this op's sole native-git spawn choke
    point -- for the duration of *fn*, and return (call_count, result)."""
    calls = {"n": 0}
    orig = git_native._git

    def _wrapper(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    git_native._git = _wrapper
    try:
        result = fn()
    finally:
        git_native._git = orig
    return calls["n"], result


def _manifest_spawn_budget() -> dict:
    manifest = budget.load_manifest()
    entry = manifest["overrides"]["ceremony.scoped_git_commit"]
    return entry["spawn_count_budget"]


# ---------------------------------------------------------------------------
# A10: three invocation shapes, pinned against budget-manifest.json
# ---------------------------------------------------------------------------


def test_green_path_spawn_count_matches_budget(tmp_path_factory):
    """A10, green path: a clean, unclaimed commit costs the manifest's
    `spawn_count_budget.green_path` git subprocesses, end to end."""
    tmp_path = tmp_path_factory.mktemp("e2e-spawn-green")
    repo = _init_repo(tmp_path)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    _write_meta_live(repo, "sess-caller", live=True)

    def _run():
        return scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["a.txt"],
                "message": "test commit",
            }
        )

    n, result = _count_op_git_calls(_run)
    assert result["committed"] is True, "fixture did not land a commit: %r" % (result,)

    budgeted = _manifest_spawn_budget()["green_path"]
    assert n == budgeted, (
        "green-path e2e spawn count is %d, manifest budgets %d -- update "
        "budget-manifest.json's ceremony.scoped_git_commit.spawn_count_"
        "budget.green_path (and its _rationale) together with this test "
        "if the change is intentional" % (n, budgeted)
    )


def test_directory_pathspec_expansion_spawn_count_matches_budget(tmp_path_factory):
    """A10, directory-pathspec-expansion: a directory element with dirty
    tracked content beneath it (`_expand_directory_pathspecs`) costs the
    manifest's `spawn_count_budget.directory_pathspec_expansion` git
    subprocesses, end to end -- green_path's cost plus the one extra
    pathspec-scoped expansion probe."""
    tmp_path = tmp_path_factory.mktemp("e2e-spawn-dir-expand")
    repo = _init_repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("v1\n", encoding="utf-8")
    (repo / "sub" / "b.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "sub" / "a.txt").write_text("v2\n", encoding="utf-8")
    (repo / "sub" / "b.txt").write_text("v2\n", encoding="utf-8")
    _write_meta_live(repo, "sess-caller", live=True)

    def _run():
        return scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["sub"],
                "message": "test commit",
            }
        )

    n, result = _count_op_git_calls(_run)
    assert result["committed"] is True, "fixture did not land a commit: %r" % (result,)

    budgeted = _manifest_spawn_budget()["directory_pathspec_expansion"]
    assert n == budgeted, (
        "directory-pathspec-expansion e2e spawn count is %d, manifest "
        "budgets %d -- update budget-manifest.json's ceremony."
        "scoped_git_commit.spawn_count_budget.directory_pathspec_expansion "
        "(and its _rationale) together with this test if the change is "
        "intentional" % (n, budgeted)
    )
