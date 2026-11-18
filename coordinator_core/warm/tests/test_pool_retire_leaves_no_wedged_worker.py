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

from coordinator_core.warm import server

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
