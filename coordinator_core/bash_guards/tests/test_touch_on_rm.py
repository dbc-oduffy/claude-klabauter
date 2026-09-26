
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
from coordinator_core.win_portability import no_console_creationflags

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
        **no_console_creationflags(),
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
