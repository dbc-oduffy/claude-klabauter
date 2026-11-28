"""Retire a warm `ProcessPoolExecutor` without a mid-lock worker kill or a forever-joined management thread."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Callable, Optional

from coordinator_core.warm import pool_orphans

_THREAD_JOIN_SECS = 2.0


def retire_pool(
    pool: Any,
    *,
    grace_secs: float,
    on_done: Optional[Callable[[], None]] = None,
    ledger: Optional[list] = None,
) -> None:
    """Retire `pool`: every worker gone, no executor management thread left joining.

    Returns at once and never joins on the caller thread; `on_done` runs once the pool is
    fully retired. Raises nothing. Idle workers exit on their shutdown sentinel; after
    `grace_secs` the survivors are terminated together (a lone terminated holder of the shared
    call-queue lock would wedge every sibling). `ledger` receives the retired worker handles
    synchronously, so the caller's liveness count never misses a worker mid-retire.
    """
    try:
        procs = list((getattr(pool, "_processes", None) or {}).values())
        if ledger is not None:
            ledger.extend(procs)
        # `shutdown` drops the executor's references to these; take them first.
        mgmt = getattr(pool, "_executor_manager_thread", None)
        queues = [getattr(pool, name, None) for name in ("_call_queue", "_result_queue")]
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 -- a broken executor still gets its workers retired
            pass
        threading.Thread(
            target=_finish, args=(mgmt, queues, procs, grace_secs, on_done), daemon=True, name="pool-retire"
        ).start()
    except Exception:  # noqa: BLE001
        pass


class WorkerRetired(RuntimeError):
    """The exception a retired worker's in-flight future is resolved with; never a `BrokenProcessPool`."""


_MANAGER_SETTLE_SECS = 1.0
_MANAGER_POLL_SECS = 0.01
_MANAGER_REBUILD_SECS = 0.05


def retire_worker(
    pool: Any,
    pid: int,
    *,
    future: Optional[Any] = None,
    on_done: Optional[Callable[[], None]] = None,
    ledger: Optional[list] = None,
) -> bool:
    """Retire the one worker `pid` of `pool` while its peers keep serving; True when it was taken out.

    trap: safe only for a worker mid-op; an idle worker may own the shared call-queue lock.
    """
    try:
        procs = getattr(pool, "_processes", None)
        if not isinstance(procs, dict) or pid not in procs:
            return False
        if not getattr(pool, "_safe_to_dynamically_spawn_children", False):
            return False
        with getattr(pool, "_shutdown_lock", None) or contextlib.nullcontext():
            proc = procs.pop(pid, None)
        if proc is None:
            return False
        if ledger is not None:
            ledger.append(proc)
        _resolve_retired_future(pool, future)
        finish_args = (
            proc,
            getattr(pool, "_executor_manager_thread_wakeup", None),
            getattr(pool, "_executor_manager_thread", None),
            on_done,
        )
        try:
            threading.Thread(target=_finish_worker, args=finish_args, daemon=True, name="worker-retire").start()
        except Exception:  # noqa: BLE001 -- no thread to spare: terminate inline, never leave a live unlisted worker
            _finish_worker(*finish_args)
        return True
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("retire_worker failed for pid %s: %s", pid, type(exc).__name__)
        return False


def _resolve_retired_future(pool: Any, future: Optional[Any]) -> None:
    """Drop a running `future` from the executor's pending items, then fail it with `WorkerRetired`."""
    if future is None:
        return
    try:
        if future.running():
            items = getattr(pool, "_pending_work_items", None)
            if isinstance(items, dict):
                for work_id, item in list(items.items()):
                    if getattr(item, "future", None) is future:
                        items.pop(work_id, None)
        if not future.done():
            future.set_exception(WorkerRetired("worker retired while holding this op"))
    except Exception:  # noqa: BLE001 -- the future resolved on its own in the meantime
        pass


def _finish_worker(proc: Any, wakeup: Any, mgmt: Any, on_done: Optional[Callable[[], None]]) -> None:
    try:
        _let_manager_forget(wakeup, mgmt)
        if _alive(proc):
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        _join(proc, time.monotonic() + _THREAD_JOIN_SECS)
        if _alive(proc) and getattr(proc, "pid", None) is not None:
            pool_orphans.reap_orphans(frozenset({proc.pid}), frozenset())
    except Exception:  # noqa: BLE001
        pass
    finally:
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001
                pass


def _let_manager_forget(wakeup: Any, mgmt: Any) -> None:
    """Wake the executor manager and wait, bounded, until it has consumed the wake-up: its next wait
    is built from `_processes`, which no longer lists the retiring worker."""
    if wakeup is None or mgmt is None or not mgmt.is_alive():
        return
    wakeup.wakeup()
    reader = getattr(wakeup, "_reader", None)
    deadline = time.monotonic() + _MANAGER_SETTLE_SECS
    while reader is not None and time.monotonic() < deadline:
        try:
            if not reader.poll():
                break
        except Exception:  # noqa: BLE001
            break
        time.sleep(_MANAGER_POLL_SECS)
    time.sleep(_MANAGER_REBUILD_SECS)


def _finish(
    mgmt: Any, queues: list, procs: list, grace_secs: float, on_done: Optional[Callable[[], None]]
) -> None:
    try:
        _cancel_feeder_joins(queues)
        deadline = time.monotonic() + max(0.0, grace_secs)
        for proc in procs:
            _join(proc, deadline)
        survivors = [p for p in procs if _alive(p)]
        for proc in survivors:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        if mgmt is not None and mgmt is not threading.current_thread():
            mgmt.join(_THREAD_JOIN_SECS)
        if survivors:
            pool_orphans.reap_orphans(
                frozenset(p.pid for p in survivors if _alive(p) and p.pid is not None), frozenset()
            )
    except Exception:  # noqa: BLE001
        pass
    finally:
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001
                pass


def _cancel_feeder_joins(queues: list) -> None:
    """Make the executor's queue feeder threads unjoinable-forever: a feeder blocked on a pipe whose
    reader is gone must not hold the management thread's `join_thread`."""
    for queue in queues:
        try:
            queue.cancel_join_thread()
        except Exception:  # noqa: BLE001
            pass


def _join(proc: Any, deadline: float) -> None:
    try:
        proc.join(max(0.0, deadline - time.monotonic()))
    except Exception:  # noqa: BLE001
        pass


def _alive(proc: Any) -> bool:
    try:
        return bool(proc.is_alive())
    except Exception:  # noqa: BLE001 -- a handle that cannot be polled is terminated, not trusted gone
        return True
