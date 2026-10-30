"""An abandoned in-process index writer leaves no index.lock, and acquire recovers a dead one.

Covers `index_write.prepare_splice` / `PreparedSplice` and the `commit_v2` splice block in
`commit.py`: a deadline-abandoned dispatch, a dropped PreparedSplice and a lock whose owner is
provably gone never block the next commit; a live holder still raises `IndexWriteLockBusy`.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.git import commit, index_write
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "seed.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    return root


def _updates(repo):
    blob = _git(["hash-object", "-w", "--", "new.txt"], repo).strip()
    return {"new.txt": (0o100644, blob)}


def _lock(repo) -> Path:
    return repo / ".git" / "index.lock"


def test_dropped_prepared_splice_frees_its_lock(repo):
    prepared = index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
    assert _lock(repo).exists()
    del prepared
    gc.collect()
    assert not _lock(repo).exists()
    assert not index_write._owner_path(_lock(repo)).exists()


def test_dead_thread_holder_is_recovered_at_acquire(repo):
    box = []

    def writer():
        box.append(index_write.prepare_splice(repo, _updates(repo), stat_fallback=True))

    t = threading.Thread(target=writer)
    t.start()
    t.join()
    keep = box[0]  # still referenced: only the dead thread proves the writer gone
    assert _lock(repo).exists()
    prepared = index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
    prepared.publish()
    assert not _lock(repo).exists()
    held = index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
    keep.abort()  # the reaped zombie must not delete the successor's lock
    assert _lock(repo).exists()
    held.abort()


def test_dead_pid_owner_is_recovered(repo):
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **no_console_creationflags())
    proc.wait()
    _lock(repo).write_bytes(b"x" * 3145728)
    index_write._owner_path(_lock(repo)).write_text(
        json.dumps({"pid": proc.pid, "token": "gone"}), encoding="utf-8"
    )
    index_write.prepare_splice(repo, _updates(repo), stat_fallback=True).publish()
    assert not _lock(repo).exists()
    assert "new.txt" in _git(["ls-files"], repo)


def test_live_holder_still_busy_and_index_untouched(repo):
    before = (repo / ".git" / "index").read_bytes()
    held = index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
    try:
        with pytest.raises(index_write.IndexWriteLockBusy):
            index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
        assert _lock(repo).exists()
    finally:
        held.abort()
    assert (repo / ".git" / "index").read_bytes() == before


def test_live_foreign_pid_owner_is_busy(repo):
    _lock(repo).write_bytes(b"partial")
    index_write._owner_path(_lock(repo)).write_text(
        json.dumps({"pid": os.getppid(), "token": "t"}), encoding="utf-8"
    )
    if os.getppid() in (0, os.getpid()):
        pytest.skip("no distinct live pid available")
    with pytest.raises(index_write.IndexWriteLockBusy):
        index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
    assert _lock(repo).exists()


def test_ownerless_lock_is_never_reaped_by_acquire(repo):
    _lock(repo).write_bytes(b"git.exe's")
    with pytest.raises(index_write.IndexWriteLockBusy):
        index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
    assert _lock(repo).exists()


def test_commit_abandoned_before_ref_move_leaves_no_lock(repo, monkeypatch):
    head = _git(["rev-parse", "HEAD"], repo).strip()
    real = commit._prepare_index_splice

    def prepare_then_expire(*a, **kw):
        ev = threading.Event()
        ev.set()
        dispatch_abandon.ACTIVE.set(ev)
        return real(*a, **kw)

    monkeypatch.setattr(commit, "_prepare_index_splice", prepare_then_expire)
    token = dispatch_abandon.ACTIVE.set(None)
    try:
        with pytest.raises(commit.CommitRefused):
            commit.commit_paths(
                repo, ["new.txt"], "land\n",
                blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
            )
    finally:
        dispatch_abandon.ACTIVE.reset(token)
    assert not _lock(repo).exists()
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
