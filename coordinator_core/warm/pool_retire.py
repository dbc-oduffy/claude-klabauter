"""Retire a warm `ProcessPoolExecutor` without a mid-lock worker kill or a forever-joined management thread."""

from __future__ import annotations

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
