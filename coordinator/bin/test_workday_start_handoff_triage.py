"""test_workday_start_handoff_triage.py — path-scoped test suite for
`workday-start-handoff-triage.py`'s `stale-plans` batching (chunk C15 of
docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md).

Covers `_git_last_commit_epochs_batch` (the batched multi-pathspec git-log
walk that replaced the former per-plan `git log -1 -- <path>` N+1 spawn) and
`find_stale_executing_plans` end-to-end, including: parity with the previous
per-path semantics, absence reconciliation (never-committed / untracked
paths map to None rather than being silently dropped), and the deliberate
prefix-match / age-threshold behavior preserved from the bash original.

Runs bash-free: `python3 test_workday_start_handoff_triage.py` (or via the
coordinator test runner). Exit 0 = all tests pass; non-zero = at least one
failure.

Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a § C15
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time

import pytest

# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
# not the route for a new file -- test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PASS = 0
FAIL = 0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _pass(label: str) -> None:
    global PASS
    print(f"  PASS: {label}")
    PASS += 1


def _fail(label: str, detail: str = "") -> None:
    """Fail the enclosing test.

    Negative-spec: this MUST raise. It previously only printed and bumped a
    module-global counter that nothing ever asserted on, which made every
    check in this file decorative. Do not "restore" the counting-only shape.
    """
    global FAIL
    print(f"  FAIL: {label}")
    if detail:
        print(f"    {detail}")
    FAIL += 1
    pytest.fail(f"{label}: {detail}" if detail else label, pytrace=False)


def _load_module():
    """Import workday-start-handoff-triage.py as a fresh module object."""
    path = os.path.join(SCRIPT_DIR, "workday-start-handoff-triage.py")
    spec = importlib.util.spec_from_file_location("workday_start_handoff_triage_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _git(repo_dir, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


def _init_repo(repo_dir):
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")


def _write_and_commit(repo_dir, rel_path, content, message):
    full = repo_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "-q", "-m", message)


def _plan_body(status="executing"):
    return f"---\nstatus: {status}\n---\n\nbody\n"


# ===========================================================================
# _git_last_commit_epochs_batch: single multi-pathspec walk, matches
# `git log -1 -- <path>` per-path semantics.
# ===========================================================================
def test_batch_resolves_most_recent_commit_per_path(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"

    _write_and_commit(tmp_path, "docs/plans/a.md", _plan_body(), "add a")
    time.sleep(1.1)
    _write_and_commit(tmp_path, "docs/plans/b.md", _plan_body(), "add b")
    time.sleep(1.1)
    _write_and_commit(tmp_path, "docs/plans/a.md", _plan_body() + "more\n", "touch a again")

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        a_path = plans_dir / "a.md"
        b_path = plans_dir / "b.md"
        result = mod._git_last_commit_epochs_batch([a_path, b_path])
    finally:
        os.chdir(cwd)

    if result[a_path] is None or result[b_path] is None:
        _fail(
            "test_batch_resolves_most_recent_commit_per_path",
            f"expected both resolved, got {result}",
        )
        return
    if result[a_path] <= result[b_path]:
        _fail(
            "test_batch_resolves_most_recent_commit_per_path",
            f"expected a's re-touch commit to be newer than b: {result}",
        )
        return
    _pass("test_batch_resolves_most_recent_commit_per_path")


def test_batch_never_committed_path_maps_to_none_not_dropped(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    _write_and_commit(tmp_path, "docs/plans/a.md", _plan_body(), "add a")

    untracked = plans_dir / "never-committed.md"
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text(_plan_body())

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = mod._git_last_commit_epochs_batch([plans_dir / "a.md", untracked])
    finally:
        os.chdir(cwd)

    if untracked not in result:
        _fail(
            "test_batch_never_committed_path_maps_to_none_not_dropped",
            "absent path silently dropped from result dict instead of reconciled to None",
        )
        return
    if result[untracked] is not None:
        _fail(
            "test_batch_never_committed_path_maps_to_none_not_dropped",
            f"expected None for never-committed path, got {result[untracked]}",
        )
        return
    if result[plans_dir / "a.md"] is None:
        _fail(
            "test_batch_never_committed_path_maps_to_none_not_dropped",
            "committed path unexpectedly resolved to None",
        )
        return
    _pass("test_batch_never_committed_path_maps_to_none_not_dropped")


def test_batch_conflict_resolution_merge_commit_is_not_stale(tmp_path):
    """Regression for the merge-suppression trap: `git log --name-only`
    prints NO file-list line for a merge commit by default, even one that
    survives history simplification under a pathspec (i.e. genuinely
    touched the path via conflict resolution) — so without
    `--diff-merges=first-parent`, the batched matcher would skip past the
    merge's header (real, current `%ct`) straight to the next, OLDER commit
    that does print a name line, silently returning a stale timestamp. This
    pins that the merge commit's own epoch is returned instead.
    """
    mod = _load_module()
    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "main")
    plans_dir = tmp_path / "docs" / "plans"

    _write_and_commit(tmp_path, "docs/plans/conflict.md", "base\n", "add base")
    time.sleep(1.1)

    _git(tmp_path, "checkout", "-b", "side")
    (plans_dir / "conflict.md").write_text("side change\n")
    _git(tmp_path, "add", "docs/plans/conflict.md")
    _git(tmp_path, "commit", "-q", "-m", "side edit")
    time.sleep(1.1)

    _git(tmp_path, "checkout", "main")
    (plans_dir / "conflict.md").write_text("trunk change\n")
    _git(tmp_path, "add", "docs/plans/conflict.md")
    _git(tmp_path, "commit", "-q", "-m", "trunk edit")
    time.sleep(1.1)

    merge = subprocess.run(
        ["git", "-C", str(tmp_path), "merge", "--no-ff", "--no-commit", "side"],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    if merge.returncode == 0:
        _fail(
            "test_batch_conflict_resolution_merge_commit_is_not_stale",
            "expected a real conflict to set up this fixture",
        )
        return

    (plans_dir / "conflict.md").write_text("resolved\n")
    _git(tmp_path, "add", "docs/plans/conflict.md")
    _git(tmp_path, "commit", "-q", "-m", "merge: resolve conflict")

    merge_epoch = int(_git(tmp_path, "log", "-1", "--format=%ct").strip())

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = mod._git_last_commit_epochs_batch([plans_dir / "conflict.md"])
    finally:
        os.chdir(cwd)

    resolved = result.get(plans_dir / "conflict.md")
    if resolved != merge_epoch:
        _fail(
            "test_batch_conflict_resolution_merge_commit_is_not_stale",
            f"expected merge commit's own epoch {merge_epoch}, got {resolved}",
        )
        return
    _pass("test_batch_conflict_resolution_merge_commit_is_not_stale")


def test_batch_empty_input_returns_empty_dict(tmp_path):
    mod = _load_module()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = mod._git_last_commit_epochs_batch([])
    finally:
        os.chdir(cwd)
    if result != {}:
        _fail("test_batch_empty_input_returns_empty_dict", f"expected {{}}, got {result}")
        return
    _pass("test_batch_empty_input_returns_empty_dict")


# ===========================================================================
# find_stale_executing_plans: end-to-end, batched-path behavior parity.
# ===========================================================================
def test_find_stale_executing_plans_flags_old_executing_plan(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    _write_and_commit(tmp_path, "docs/plans/old.md", _plan_body("executing"), "add old")

    old_epoch = int(_git(tmp_path, "log", "-1", "--format=%ct").strip())
    now = old_epoch + (5 * 86400)  # 5 days later, threshold default is 3

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        results = mod.find_stale_executing_plans(now=float(now))
    finally:
        os.chdir(cwd)

    if not results or "old.md" not in results[0]:
        _fail(
            "test_find_stale_executing_plans_flags_old_executing_plan",
            f"expected old.md flagged as stale, got {results}",
        )
        return
    _pass("test_find_stale_executing_plans_flags_old_executing_plan")


def test_find_stale_executing_plans_skips_non_executing(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    _write_and_commit(tmp_path, "docs/plans/done.md", _plan_body("complete"), "add done")

    old_epoch = int(_git(tmp_path, "log", "-1", "--format=%ct").strip())
    now = old_epoch + (10 * 86400)

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        results = mod.find_stale_executing_plans(now=float(now))
    finally:
        os.chdir(cwd)

    if results:
        _fail("test_find_stale_executing_plans_skips_non_executing", f"expected [], got {results}")
        return
    _pass("test_find_stale_executing_plans_skips_non_executing")


def test_find_stale_executing_plans_skips_never_committed(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    # A repo needs at least one commit to have a valid HEAD for `git log`.
    _write_and_commit(tmp_path, "README.md", "seed\n", "seed commit")

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "untracked.md").write_text(_plan_body("executing"))

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        results = mod.find_stale_executing_plans(now=time.time() + 10 * 86400)
    finally:
        os.chdir(cwd)

    if results:
        _fail(
            "test_find_stale_executing_plans_skips_never_committed",
            f"expected [] (untracked path treated as unresolved, not stale), got {results}",
        )
        return
    _pass("test_find_stale_executing_plans_skips_never_committed")


def test_find_stale_executing_plans_multiple_plans_all_resolved(tmp_path):
    """Parity check: with several executing plans in one directory, the
    batched walk must resolve every one independently (no cross-path
    contamination from the shared single git-log call)."""
    mod = _load_module()
    _init_repo(tmp_path)
    for name in ("p1.md", "p2.md", "p3.md"):
        _write_and_commit(tmp_path, f"docs/plans/{name}", _plan_body("executing"), f"add {name}")
        time.sleep(1.1)

    old_epoch = int(_git(tmp_path, "log", "-1", "--format=%ct").strip())
    now = old_epoch + (10 * 86400)

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        results = mod.find_stale_executing_plans(now=float(now))
    finally:
        os.chdir(cwd)

    flagged = {line for line in results}
    if len(flagged) != 3:
        _fail(
            "test_find_stale_executing_plans_multiple_plans_all_resolved",
            f"expected all 3 plans flagged stale, got {results}",
        )
        return
    _pass("test_find_stale_executing_plans_multiple_plans_all_resolved")


def _main() -> int:
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    import tempfile
    from pathlib import Path

    for test_fn in tests:
        with tempfile.TemporaryDirectory() as td:
            test_fn(Path(td))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
