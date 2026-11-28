"""A retired warm pool leaves no live worker process and no live executor management thread.

Shape: a real 2-worker `ProcessPoolExecutor` where one worker holds the call-queue rlock (parked
mid-lock) and its sibling is blocked acquiring it, then each retire path of `_ServerContext`.
"""

from __future__ import annotations

import concurrent.futures
import gc
import multiprocessing
import multiprocessing.queues
import os
import threading
import time
from pathlib import Path

import psutil
import pytest

from coordinator_core.warm import pool_retire, server

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SETTLE_SECS = 5.0
_PATHS = ("stalled_pool", "wedged_guard_pool", "overdue_worker")


def _hold_call_queue_rlock(claim: str, park_secs: float) -> int:
    """Worker task: record the pid, take this worker's call-queue rlock, park holding it."""
    Path(claim).write_text(str(os.getpid()), encoding="ascii")
    for obj in gc.get_objects():
        if isinstance(obj, multiprocessing.queues.Queue) and hasattr(obj, "_rlock"):
            obj._rlock.acquire()
            break
    time.sleep(park_secs)
    return os.getpid()


def _noop() -> int:
    return os.getpid()


def _wedged_pool(tmp_path: Path):
    """2-worker pool: worker A parked holding the call-queue rlock, worker B blocked acquiring it."""
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context())
    claim = tmp_path / "claim"
    holder = pool.submit(_hold_call_queue_rlock, str(claim), 600.0)
    deadline = time.monotonic() + 30
    while not claim.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert claim.exists(), "holder never started"
    time.sleep(0.5)
    sibling = pool.submit(_noop)
    time.sleep(1.0)
    assert len(pool._processes) == 2 and not holder.done() and not sibling.done()
    return pool, claim, holder


def _ctx(tmp_path: Path, pool, *, guard: bool):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._guard_pool_lock = threading.Lock()
    ctx._dispatch_pool = None if guard else pool
    ctx._guard_pool = pool if guard else None
    ctx._stall_dir = tmp_path / "stall-dumps"
    ctx.engine_root = tmp_path
    return ctx


def _retire(path: str, ctx, pool, claim: Path, holder) -> None:
    if path == "stalled_pool":
        ctx._recycle_stalled_pool(pool, ["review.reverify"])
    elif path == "wedged_guard_pool":
        ctx.__dict__["_guard_overdue"] = {object() for _ in range(server._guard_pool_workers())}
        assert ctx._recycle_wedged_guard_pool(pool)
    else:
        assert ctx._terminate_overdue_worker(pool, claim, None, overdue_future=holder)


def _live_children(baseline: set) -> set:
    return {c.pid for c in psutil.Process().children(recursive=True) if c.is_running()} - baseline


@pytest.mark.parametrize("path", _PATHS)
def test_retire_leaves_no_live_worker_and_no_management_thread(path, tmp_path, monkeypatch):
    # The sibling here can never drain (the holder owns its lock), so the overdue path's
    # neighbour wait runs to its bound; shrink the bound below the settle window.
    monkeypatch.setattr(server, "_OVERDUE_REAP_GRACE_SECS", 1.0)
    baseline = {c.pid for c in psutil.Process().children(recursive=True)}
    pool, claim, holder = _wedged_pool(tmp_path)
    mgmt = pool._executor_manager_thread
    spawned = _live_children(baseline)
    assert len(spawned) >= 2
    try:
        _retire(path, _ctx(tmp_path, pool, guard=path == "wedged_guard_pool"), pool, claim, holder)
        time.sleep(_SETTLE_SECS)
        orphans = _live_children(baseline)
        mgmt_alive = int(mgmt is not None and mgmt.is_alive())
        print(f"BASE-COUNTS path={path} orphans={len(orphans)} mgmt_threads={mgmt_alive}")
        assert (len(orphans), mgmt_alive) == (0, 0), f"{path}: orphans={sorted(orphans)} mgmt_alive={mgmt_alive}"
    finally:
        for pid in _live_children(baseline):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        _release_self_deadlocked_manager(pool, mgmt)


def _park(claim: str, park_secs: float) -> int:
    """Worker task: record the pid and block as an IO-held worker does, holding no shared lock."""
    Path(claim).write_text(str(os.getpid()), encoding="ascii")
    time.sleep(park_secs)
    return os.getpid()


def _two_worker_pool_with_a_held_worker(tmp_path: Path):
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context())
    claim = tmp_path / "claim"
    holder = pool.submit(_park, str(claim), 600.0)
    deadline = time.monotonic() + 30
    while not claim.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert claim.exists(), "holder never started"
    time.sleep(0.2)
    return pool, int(claim.read_text(encoding="ascii")), holder


def test_one_held_worker_is_retired_alone_and_the_pool_keeps_serving(tmp_path):
    baseline = {c.pid for c in psutil.Process().children(recursive=True)}
    pool, held_pid, holder = _two_worker_pool_with_a_held_worker(tmp_path)
    try:
        peer_pid = pool.submit(_noop).result(60)
        assert peer_pid != held_pid and len(pool._processes) == 2
        gone, ledger = threading.Event(), []
        assert pool_retire.retire_worker(pool, held_pid, future=holder, on_done=gone.set, ledger=ledger)
        assert gone.wait(30), "the retired worker never finished retiring"
        assert held_pid not in pool._processes and not ledger[0].is_alive()
        assert holder.done() and isinstance(holder.exception(), pool_retire.WorkerRetired)
        assert not psutil.pid_exists(held_pid) or not psutil.Process(held_pid).is_running()
        # Not broken: submit finds a free slot and serves, and the peer is the same live worker.
        first, second = pool.submit(_park, str(tmp_path / "c1"), 3.0), pool.submit(_noop)
        assert held_pid not in pool._processes and len(pool._processes) == 2
        assert second.result(60) != held_pid
        assert peer_pid in pool._processes and psutil.Process(peer_pid).is_running()
        assert first.result(60) != held_pid
    finally:
        for pid in {c.pid for c in psutil.Process().children(recursive=True)} - baseline:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        pool.shutdown(wait=False, cancel_futures=True)


def test_retire_worker_refuses_a_pid_that_is_not_in_the_pool_and_a_pool_that_cannot_respawn(tmp_path):
    baseline = {c.pid for c in psutil.Process().children(recursive=True)}
    pool, held_pid, holder = _two_worker_pool_with_a_held_worker(tmp_path)
    try:
        before = dict(pool._processes)
        assert pool_retire.retire_worker(pool, held_pid + 100_000, future=holder) is False
        pool._safe_to_dynamically_spawn_children = False
        assert pool_retire.retire_worker(pool, held_pid, future=holder) is False
        assert dict(pool._processes) == before and not holder.done()
    finally:
        for pid in {c.pid for c in psutil.Process().children(recursive=True)} - baseline:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        pool.shutdown(wait=False, cancel_futures=True)


class _FakeProc:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = 0

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated += 1
        self.alive = False

    def join(self, timeout=None) -> None:
        pass


class _FakePool:
    _safe_to_dynamically_spawn_children = True

    def __init__(self, proc: _FakeProc) -> None:
        self._processes = {4242: proc}


def test_retire_worker_terminates_inline_when_the_helper_thread_cannot_start(monkeypatch):
    proc, done = _FakeProc(), []
    pool = _FakePool(proc)

    def _no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _no_thread)
    assert pool_retire.retire_worker(pool, 4242, on_done=lambda: done.append(1)) is True
    assert 4242 not in pool._processes and proc.terminated == 1 and not proc.alive and done == [1]


def test_retire_worker_leaves_a_fake_worker_to_its_helper_thread_when_threads_start(monkeypatch):
    proc, gone = _FakeProc(), threading.Event()
    pool = _FakePool(proc)
    assert pool_retire.retire_worker(pool, 4242, on_done=gone.set) is True
    assert gone.wait(10) and proc.terminated == 1 and 4242 not in pool._processes


def _release_self_deadlocked_manager(pool, mgmt) -> None:
    """A manager thread left blocked on its own executor's `_shutdown_lock` would hang interpreter exit."""
    if mgmt is None:
        return
    mgmt.join(2.0)
    if mgmt.is_alive():
        try:
            pool._shutdown_lock.release()
        except RuntimeError:
            pass
        mgmt.join(5.0)
