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


def _hold_in_thread(repo, event):
    """Take the lock in a thread bound to `event`; returns (thread, box, release)."""
    box, release = [], threading.Event()

    def writer():
        token = dispatch_abandon.ACTIVE.set(event)
        try:
            box.append(index_write.prepare_splice(repo, _updates(repo), stat_fallback=True))
            release.wait(10)
        finally:
            dispatch_abandon.ACTIVE.reset(token)

    t = threading.Thread(target=writer)
    t.start()
    while not box:
        t.join(0.01)
    return t, box, release


def test_abandoned_but_alive_holder_is_reclaimed_at_acquire(repo):
    t, box, release = _hold_in_thread(repo, threading.Event())
    try:
        with pytest.raises(index_write.IndexWriteLockBusy):
            index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
        assert _lock(repo).exists()
        # The same live thread, now told its op timed out.
        for h in index_write._HOLDERS.values():
            h.abandon.set()
        successor = index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
        assert t.is_alive()
        assert not box[0].held()
        with pytest.raises(index_write.IndexWriteError):
            box[0].publish()
        assert _lock(repo).exists()  # the zombie's refusal did not delete the successor's lock
        successor.publish()
        assert "new.txt" in _git(["ls-files"], repo)
    finally:
        release.set()
        t.join()


def test_abandoned_holder_already_publishing_is_not_reclaimed(repo):
    ev = threading.Event()
    t, box, release = _hold_in_thread(repo, ev)
    try:
        assert box[0]._begin_publish()
        ev.set()
        with pytest.raises(index_write.IndexWriteLockBusy):
            index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
        assert _lock(repo).exists()
    finally:
        release.set()
        t.join()
        box[0].abort()


def test_commit_abandoned_while_holding_lock_in_ref_move_is_reclaimed(repo, monkeypatch):
    head = _git(["rev-parse", "HEAD"], repo).strip()
    real_cas = commit.cas_ref
    in_cas, gate, result = threading.Event(), threading.Event(), {}
    ev = threading.Event()

    def gated_cas(*a, **kw):
        in_cas.set()
        gate.wait(10)
        return real_cas(*a, **kw)

    monkeypatch.setattr(commit, "cas_ref", gated_cas)

    def run():
        token = dispatch_abandon.ACTIVE.set(ev)
        try:
            result["out"] = commit.commit_paths(
                repo, ["new.txt"], "land\n",
                blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
            )
        except BaseException as exc:  # noqa: BLE001 -- the assertion reads it
            result["exc"] = exc
        finally:
            dispatch_abandon.ACTIVE.reset(token)

    t = threading.Thread(target=run)
    t.start()
    try:
        assert in_cas.wait(10)
        assert _lock(repo).exists()
        ev.set()  # dispatcher's budget expired; the handler thread is still alive in cas_ref
        successor = index_write.prepare_splice(repo, _updates(repo), stat_fallback=True)
    finally:
        gate.set()
        t.join(30)
    try:
        assert not t.is_alive()
        # The ref moved before the abandonment mattered: landed, never rolled back.
        assert _git(["rev-parse", "HEAD"], repo).strip() != head
        exc = result.get("exc")
        assert exc is None or isinstance(exc, index_write.IndexStaleAfterCommit)
        assert _lock(repo).exists()  # the successor's lock survived the zombie's publish
    finally:
        successor.abort()
    assert not _lock(repo).exists()


def test_commit_abandoned_after_lock_taken_releases_it_before_ref_move(repo, monkeypatch):
    head = _git(["rev-parse", "HEAD"], repo).strip()
    real = commit._prepare_index_splice

    def prepare_then_expire(*a, **kw):
        prepared = real(*a, **kw)
        dispatch_abandon.ACTIVE.get().set()
        return prepared

    monkeypatch.setattr(commit, "_prepare_index_splice", prepare_then_expire)
    token = dispatch_abandon.ACTIVE.set(threading.Event())
    try:
        with pytest.raises(commit.CommitRefused, match="before the ref update"):
            commit.commit_paths(
                repo, ["new.txt"], "land\n",
                blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
            )
    finally:
        dispatch_abandon.ACTIVE.reset(token)
    assert not _lock(repo).exists()
    assert not index_write._owner_path(_lock(repo)).exists()
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


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
