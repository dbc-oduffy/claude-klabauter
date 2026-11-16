"""ref_lock: typed refusal, dead-writer recovery (complete / move aside), restart sweep, abandon release.

Locks are planted in tmp_path gitdirs built without spawning git; commits come from
`git_objects.write_object`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.git import git_objects, ref_lock
from coordinator_core.win_portability import no_console_creationflags

REF = "refs/heads/work/b"
LONG_AGO = 3600


def _commit(gitdir: Path, parent: str | None, msg: str) -> str:
    lines = ["tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904"]
    if parent:
        lines.append(f"parent {parent}")
    lines += ["author t <t@e> 1 +0000", "committer t <t@e> 1 +0000", "", msg, ""]
    return git_objects.write_object(gitdir, b"commit", "\n".join(lines).encode())


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **no_console_creationflags())
    proc.wait()
    return proc.pid


def _plant(gitdir: Path, content: str, *, pid: int | None, age: int = LONG_AGO) -> Path:
    lock = gitdir / (REF + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(content.encode("ascii"))
    old = time.time() - age
    os.utime(lock, (old, old))
    if pid is not None:
        owner = ref_lock._owner_path(gitdir, REF)
        owner.parent.mkdir(parents=True, exist_ok=True)
        owner.write_text(json.dumps({"pid": pid, "token": "t-1", "started": 1}), encoding="utf-8")
    return lock


@pytest.fixture
def gd(tmp_path: Path) -> Path:
    d = tmp_path / "gitdir"
    (d / "objects").mkdir(parents=True)
    (d / "refs" / "heads" / "work").mkdir(parents=True)
    return d


@pytest.fixture
def chain(gd: Path):
    """ref at `base`, reflog head already names `child` (the half-done update)."""
    base = _commit(gd, None, "base")
    child = _commit(gd, base, "child")
    (gd / REF).write_bytes(base.encode() + b"\n")
    log = gd / "logs" / REF
    log.parent.mkdir(parents=True)
    log.write_text(f"{base} {child} t <t@e> 1 +0000\tcommit: child\n", encoding="utf-8")
    return base, child


def _records(gd: Path) -> list[dict]:
    log = gd / ref_lock.RECOVERY_DIR / ref_lock.RECOVERY_LOG
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def test_dead_writer_lock_with_child_sha_and_reflog_head_is_completed(gd, chain) -> None:
    base, child = chain
    lock = _plant(gd, child + "\n", pid=_dead_pid())
    msg = ref_lock.recover_stale_ref_lock(gd, REF)
    assert msg and msg.startswith("completed")
    assert (gd / REF).read_bytes() == child.encode() + b"\n"
    assert not lock.exists()
    assert not ref_lock._owner_path(gd, REF).exists()
    recs = _records(gd)
    assert [r["outcome"] for r in recs] == ["completed"]


def test_dead_writer_lock_with_non_child_sha_is_moved_aside(gd, chain) -> None:
    base, _ = chain
    stranger = _commit(gd, None, "stranger")
    lock = _plant(gd, stranger + "\n", pid=_dead_pid())
    msg = ref_lock.recover_stale_ref_lock(gd, REF)
    assert msg and msg.startswith("moved-aside")
    assert (gd / REF).read_bytes() == base.encode() + b"\n"
    assert not lock.exists()
    recs = _records(gd)
    assert recs[0]["outcome"] == "moved-aside"
    assert Path(recs[0]["moved_to"]).read_text(encoding="ascii").strip() == stranger


def test_child_sha_not_matching_reflog_head_is_moved_aside(gd, chain) -> None:
    base, _ = chain
    other_child = _commit(gd, base, "other child")
    _plant(gd, other_child + "\n", pid=_dead_pid())
    msg = ref_lock.recover_stale_ref_lock(gd, REF)
    assert msg and msg.startswith("moved-aside")
    assert (gd / REF).read_bytes() == base.encode() + b"\n"


def test_empty_lock_of_dead_writer_is_moved_aside(gd, chain) -> None:
    _plant(gd, "", pid=_dead_pid())
    msg = ref_lock.recover_stale_ref_lock(gd, REF)
    assert msg and msg.startswith("moved-aside")


def test_fresh_lock_is_untouched(gd, chain) -> None:
    _, child = chain
    lock = _plant(gd, child + "\n", pid=_dead_pid(), age=1)
    assert ref_lock.recover_stale_ref_lock(gd, REF) is None
    assert lock.exists()


def test_ownerless_lock_is_untouched_even_when_old(gd, chain) -> None:
    _, child = chain
    lock = _plant(gd, child + "\n", pid=None)
    assert ref_lock.recover_stale_ref_lock(gd, REF) is None
    assert lock.exists()
    assert _records(gd) == []


def test_live_pid_lock_is_untouched(gd, chain) -> None:
    _, child = chain
    lock = _plant(gd, child + "\n", pid=_foreign_live_pid())
    assert ref_lock.recover_stale_ref_lock(gd, REF) is None
    assert lock.exists()
    assert ref_lock._owner_path(gd, REF).exists()


def _foreign_live_pid() -> int:
    return os.getppid() or 1


def test_own_pid_with_live_registered_holder_is_untouched(gd, chain) -> None:
    _, child = chain
    lock = gd / (REF + ".lock")
    lock.write_text(child + "\n", encoding="ascii")
    old = time.time() - LONG_AGO
    os.utime(lock, (old, old))
    holder = ref_lock.register_holder(gd, REF)
    try:
        assert ref_lock.recover_stale_ref_lock(gd, REF) is None
        assert lock.exists()
    finally:
        holder.close()


def test_own_pid_without_registered_holder_is_recovered(gd, chain) -> None:
    _, child = chain
    _plant(gd, child + "\n", pid=os.getpid())
    msg = ref_lock.recover_stale_ref_lock(gd, REF)
    assert msg and msg.startswith("completed")


def test_two_racing_recoverers_only_one_wins(gd, chain) -> None:
    _, child = chain
    _plant(gd, child + "\n", pid=_dead_pid())
    results: list = []
    barrier = threading.Barrier(2)

    def go() -> None:
        barrier.wait()
        results.append(ref_lock.recover_stale_ref_lock(gd, REF))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([r for r in results if r]) == 1
    assert len(_records(gd)) == 1


def test_ref_lock_held_names_path_age_and_content_never_moved_since(gd, chain) -> None:
    _, child = chain
    lock = _plant(gd, child + "\n", pid=None, age=120)
    exc = ref_lock.RefLockHeld.from_lock(gd, REF)
    text = str(exc)
    assert str(lock) in text and child in text and "age 120s" in text
    assert " moved since " not in text
    assert exc.stale is True
    assert exc.content == child
    assert isinstance(exc, RuntimeError)


def test_ref_lock_held_fresh_and_empty_is_not_stale(gd) -> None:
    _plant(gd, "", pid=None, age=0)
    exc = ref_lock.RefLockHeld.from_lock(gd, REF)
    assert exc.stale is False
    assert "content empty" in str(exc)


def test_sweep_completes_matching_dead_owner_lock_without_age_floor(gd, chain) -> None:
    _, child = chain
    lock = _plant(gd, child + "\n", pid=_dead_pid(), age=0)
    out = ref_lock.sweep_dead_owner_locks(gd)
    assert len(out) == 1 and out[0].startswith("completed")
    assert (gd / REF).read_bytes() == child.encode() + b"\n"
    assert not lock.exists()


def test_sweep_moves_aside_nonmatching_and_skips_live_owner(gd, chain) -> None:
    stranger = _commit(gd, None, "stranger")
    _plant(gd, stranger + "\n", pid=_dead_pid(), age=0)
    out = ref_lock.sweep_dead_owner_locks(gd)
    assert len(out) == 1 and out[0].startswith("moved-aside")

    live = _plant(gd, stranger + "\n", pid=_foreign_live_pid(), age=0)
    assert ref_lock.sweep_dead_owner_locks(gd) == []
    assert live.exists()


def test_sweep_on_clean_repo_is_empty_and_clears_orphan_owner_record(gd, chain) -> None:
    assert ref_lock.sweep_dead_owner_locks(gd) == []
    owner = ref_lock._owner_path(gd, REF)
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps({"pid": _dead_pid(), "token": "t", "started": 1}), encoding="utf-8")
    assert ref_lock.sweep_dead_owner_locks(gd) == []
    assert not owner.exists()


def test_release_abandoned_unlinks_lock_and_marks_holder_released(gd, chain) -> None:
    base, child = chain
    ev = threading.Event()
    tok = dispatch_abandon.ACTIVE.set(ev)
    try:
        lock = gd / (REF + ".lock")
        lock.write_text(child + "\n", encoding="ascii")
        holder = ref_lock.register_holder(gd, REF)
    finally:
        dispatch_abandon.ACTIVE.reset(tok)
    assert holder.valid()
    ev.set()
    assert not holder.valid()
    recs = ref_lock.release_abandoned(ev)
    assert len(recs) == 1 and recs[0]["outcome"] == "released-abandoned"
    assert recs[0]["reflog_ahead"] is True
    assert not lock.exists()
    assert not ref_lock._owner_path(gd, REF).exists()
    assert holder.close() is False  # released: the caller must not unlink a path a peer may own
    assert ref_lock.release_abandoned(ev) == []


def test_release_abandoned_leaves_a_peers_holder_alone(gd, chain) -> None:
    mine, peer = threading.Event(), threading.Event()
    tok = dispatch_abandon.ACTIVE.set(peer)
    try:
        lock = gd / (REF + ".lock")
        lock.write_text("", encoding="ascii")
        holder = ref_lock.register_holder(gd, REF)
    finally:
        dispatch_abandon.ACTIVE.reset(tok)
    mine.set()
    assert ref_lock.release_abandoned(mine) == []
    assert lock.exists() and holder.valid()
    assert holder.close() is True
    assert not ref_lock._owner_path(gd, REF).exists()


def test_released_holder_close_keeps_a_peers_owner_record(gd, chain) -> None:
    ev = threading.Event()
    tok = dispatch_abandon.ACTIVE.set(ev)
    try:
        (gd / (REF + ".lock")).write_text("", encoding="ascii")
        holder = ref_lock.register_holder(gd, REF)
    finally:
        dispatch_abandon.ACTIVE.reset(tok)
    ev.set()
    ref_lock.release_abandoned(ev)
    peer = ref_lock.register_holder(gd, REF)
    try:
        assert holder.close() is False
        assert ref_lock._owner_path(gd, REF).exists()
    finally:
        peer.close()
