"""C9 (2026-08-27, docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md):
`check_destructive_rm` records a TOUCH for every path THIS session's own
`rm`/`git rm` actually removes, closing the residue filed at
`state/bug-backlog/2026-08-27-git-rm-through-bash-leaves-no-touch-clai-
d0a400871a9f.yaml` -- a Bash `rm`/`git rm` fires no Write/Edit hook, so the
deleting session left no claim behind and its own legitimate deletion
rendered `owner:orphan` at `compute_scope`.

WHAT MUST NOT REGRESS. `compute_scope` Step 1 already admits a
touched-then-deleted path to `my_scope` with NO existence check (ratified
AC10, `test_tracked_then_deleted_file_hits_mtime_epoch_zero_in_
forgiveness_loop`) -- so a plain TOUCH (not a new verb) is sufficient, and
this file does not touch `compute_scope` at all. The negative-space
half -- a DENIED `rm` must record nothing, and a path this session did not
itself remove must never get a claim it did not earn -- is exactly what
`test_denied_rm_records_no_touch` and
`test_rm_does_not_touch_paths_it_did_not_target` pin.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.session.touch_record import (
    VERB_TOUCH,
    decode_line,
    iter_complete_lines,
    sink_path,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SESSION_ID = "c9-touch-on-rm-probe"


def _git(*args: str, cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    root = str(repo)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    return root


def _touched_paths(root: str, session_id: str = _SESSION_ID) -> set:
    sink = sink_path(os.path.join(root, ".git", "coordinator-sessions", session_id))
    if not sink.exists():
        return set()
    raw = sink.read_bytes()
    return {
        decode_line(line).path
        for line in iter_complete_lines(raw)
        if decode_line(line).verb == VERB_TOUCH
    }


def test_bash_rm_of_clean_tracked_file_records_a_touch(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    target = os.path.join(root, "seed.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("seed\n")
    _git("add", "seed.txt", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)

    monkeypatch.chdir(root)
    verdict = dc.check_destructive_rm(
        "rm seed.txt", session_id=_SESSION_ID, payload={}
    )
    assert verdict is None, f"a clean, committed, non-recursive rm must be ALLOWED: {verdict}"
    assert "seed.txt" in _touched_paths(root), (
        "check_destructive_rm allowed the rm but recorded no TOUCH for the "
        "path it just let through"
    )


def test_git_rm_of_clean_tracked_file_records_a_touch(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    target = os.path.join(root, "tracked.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("tracked\n")
    _git("add", "tracked.txt", cwd=root)
    _git("commit", "-qm", "tracked", cwd=root)

    monkeypatch.chdir(root)
    verdict = dc.check_destructive_rm(
        "git rm tracked.txt", session_id=_SESSION_ID, payload={}
    )
    assert verdict is None, f"git rm is treated as git-recoverable and stays ALLOWED: {verdict}"
    assert "tracked.txt" in _touched_paths(root), (
        "the `git rm` leg (staged removal, skipped by the deny ladder) must "
        "still record a TOUCH for what it actually removes"
    )


def test_git_rm_cached_does_not_touch_the_working_tree_copy(tmp_path, monkeypatch):
    """`git rm --cached` unstages but never removes the working-tree file --
    recording a TOUCH here would misrepresent a file this command never
    actually deletes."""
    root = _init_repo(tmp_path)
    target = os.path.join(root, "cached.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("cached\n")
    _git("add", "cached.txt", cwd=root)
    _git("commit", "-qm", "cached", cwd=root)

    monkeypatch.chdir(root)
    verdict = dc.check_destructive_rm(
        "git rm --cached cached.txt", session_id=_SESSION_ID, payload={}
    )
    assert verdict is None
    assert "cached.txt" not in _touched_paths(root), (
        "`git rm --cached` never deletes the working-tree file -- it must "
        "not be recorded as removed"
    )


def test_denied_rm_records_no_touch(tmp_path, monkeypatch):
    """The DENY leg (uncommitted/untracked work a recursive rm would
    destroy) must never let a touch record land for a deletion that never
    actually happens -- the whole point of flushing only at the function's
    final `return None`."""
    root = _init_repo(tmp_path)
    seed = os.path.join(root, "seed.txt")
    with open(seed, "w", encoding="utf-8") as fh:
        fh.write("seed\n")
    _git("add", "seed.txt", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)

    work = os.path.join(root, "work")
    os.makedirs(work)
    dirty = os.path.join(work, "dirty.py")
    with open(dirty, "w", encoding="utf-8") as fh:
        fh.write("# uncommitted\n")

    monkeypatch.chdir(root)
    verdict = dc.check_destructive_rm(
        "rm -rf work", session_id=_SESSION_ID, payload={}
    )
    assert (
        verdict is not None
        and verdict.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    ), f"recursive rm over untracked work must be DENIED: {verdict}"
    assert _touched_paths(root) == set(), (
        "a DENIED rm must record no touch at all -- the target was never "
        "actually removed"
    )


def test_rm_does_not_touch_paths_it_did_not_target(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    for name in ("a.txt", "b.txt"):
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write(f"{name}\n")
    _git("add", "a.txt", "b.txt", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)

    monkeypatch.chdir(root)
    verdict = dc.check_destructive_rm(
        "rm a.txt", session_id=_SESSION_ID, payload={}
    )
    assert verdict is None
    touched = _touched_paths(root)
    assert "a.txt" in touched
    assert "b.txt" not in touched, (
        "a.txt was rm'd, b.txt was not -- only the actually-removed path "
        "may get a touch"
    )
