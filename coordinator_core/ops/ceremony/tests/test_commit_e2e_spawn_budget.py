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

MEASURED SHAPES (re-baselined 2026-08-18, this repo, matching
`budget-manifest.json`'s `overrides["ceremony.scoped_git_commit"].
spawn_count_budget`):
  - green path (clean commit, no claim conflict): 17 spawns.
  - refusal path (an UNANSWERABLE path -- unverified caller identity
    conflicting with a live claimant): 0 spawns -- `_handler` returns the
    rejection before any staging call is ever issued.
  - a directory-pathspec-expansion invocation: 18 spawns -- the green
    path's 17 plus the one extra pathspec-scoped `git status --porcelain
    -- <dir>` `_expand_directory_pathspecs` issues up front.
  - the push-raced path: 20 spawns and ZERO hot-path `time.sleep`. Was 24
    and 1.0s while `_remote_sha_state` lived on it (opro-01 C-09 made it
    countable, C-01/C-02 removed it).

The first three were 12/0/13 when measured 2026-08-11. The +5 on the two
non-zero shapes is the agree-branch CAS's doubled index/HEAD blob read
(`_index_blobs`/`_head_blobs` at capture, re-read in
`_agree_branch_cas_refusal` before the agree branch's `git add`) plus
`interpret-trailers --no-divider --in-place` -- all of it landed between
that date and 2026-08-18 without moving the budget. This module carries
`pytest.mark.cadence`, so the fast tier never ran the assertion that
would have caught it the day it drifted; that, not the count, is the
part worth remembering.

These are exact-equality assertions, deliberately: the whole point of a
spawn-COUNT budget is that it does not drift quietly. A future op change
that adds or removes a spawn must update both this test and the manifest
entry in the same commit, not silently pass.
"""

from __future__ import annotations

import json
import subprocess
import time
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


# ---------------------------------------------------------------------------
# opro-01 C-09: the probe-bearing (push-raced / push-failed) path
# ---------------------------------------------------------------------------


def _init_repo_with_upstream(tmp_path: Path) -> Path:
    """A repo on a `work/*` branch with a real upstream, whose remote URL is
    then pointed at nothing.

    Every clause is load-bearing for reaching the probe:
      - `work/*` — `coordinator-auto-push` doctrine declines to push anything
        else, and `PUSH_STATUS_DECLINED` short-circuits `_resolve_push_report`
        before `_remote_sha_state`.
      - a real first push — leaves `origin/<branch>` as a resolvable local ref,
        so `merge-base --is-ancestor` can answer 1 (definitively absent) rather
        than 128 (unknown), which is what drives the full retry loop.
      - a broken remote URL afterwards — makes this op's own push fail without
        a fast-forward reject, so `_rebase_onto_fetched_ref` stays out of the
        count.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(["init", "--bare", "-q"], bare)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "work/budget/probe"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/budget/probe"], repo)
    _git(["remote", "set-url", "origin", str(tmp_path / "nonexistent.git")], repo)
    return repo


def test_push_raced_path_spawn_count_and_sleep_match_budget(tmp_path_factory):
    """C-05 after-figure: the push-raced path costs the manifest's
    `spawn_count_budget.push_raced_path` git subprocesses and sleeps ZERO.

    This is the path `_remote_sha_state` used to be reachable on -- the green
    path's fixture has no remote and no upstream, so the probe was never
    reachable there and `green_path` is unaffected by its removal. C-09 first
    routed the probe's four spawns through `git_native._git` so this counter
    could see them at all (they were bare `subprocess.run`); C-01/C-02 then
    removed the probe, taking those four spawns and the retry loop's 1.0s with
    them.

    The zero-sleep assertion is the load-bearing half. A spawn count cannot
    express a `time.sleep`, and a reintroduced retry loop would cost 1.0s per
    raced commit under a 50-70-concurrent-session load norm while showing up
    as only a small count change -- or, if it slept without spawning, as none
    at all.
    """
    tmp_path = tmp_path_factory.mktemp("e2e-spawn-probe")
    repo = _init_repo_with_upstream(tmp_path)
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    _write_meta_live(repo, "sess-caller", live=True)

    # Patched on the `time` module itself, not on any one module's imported
    # reference: C-02 removed `scoped_git_commit`'s `import time` entirely, and
    # a per-module patch would only ever catch a sleep reintroduced in the one
    # module it was aimed at. This catches one anywhere on the path.
    slept: list[float] = []
    orig_sleep = time.sleep

    def _record_sleep(secs):
        slept.append(secs)
        return orig_sleep(secs)

    def _run():
        return scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["a.txt"],
                "message": "test commit",
            }
        )

    time.sleep = _record_sleep
    try:
        n, result = _count_op_git_calls(_run)
    finally:
        time.sleep = orig_sleep

    assert result["committed"] is True, "fixture did not land a commit: %r" % (result,)
    assert result["push_state"] == "push-failed", (
        "fixture did not reach the probe -- push_state is %r, so "
        "_resolve_push_report short-circuited before _remote_sha_state and "
        "this test is measuring the wrong path" % (result.get("push_state"),)
    )

    budgeted = _manifest_spawn_budget()["push_raced_path"]
    assert n == budgeted, (
        "push-raced-path e2e spawn count is %d, manifest budgets %d -- "
        "update budget-manifest.json's ceremony.scoped_git_commit.spawn_"
        "count_budget.push_raced_path (and its _rationale) together with "
        "this test if the change is intentional" % (n, budgeted)
    )
    assert slept == [], (
        "the push-raced path slept %.2fs on the hot path, expected none -- "
        "opro-01 C-02 removed the only sleep this path had "
        "(`_remote_sha_state`'s retry loop). %r" % (sum(slept), slept)
    )
