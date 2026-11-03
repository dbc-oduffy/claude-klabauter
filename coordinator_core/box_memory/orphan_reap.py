"""Reap grep/find processes whose parent is verified dead.

Dead parent: the ppid is absent from the census and fails a liveness probe, or present with a creation time later than the
child's (the PID was reused). An unreadable census, a zero ctime or a ppid of 0 reads as alive.
No per-process spawn: one census read, then a direct terminate per orphan.
"""

from __future__ import annotations

import os
import signal
from typing import Callable, List, Optional, Sequence

from coordinator_core.box_memory import ReapPrimitives
from coordinator_core.box_memory.pressure import is_search_name


def _parent_dead(row, by_pid: dict, primitives: Optional[ReapPrimitives] = None) -> bool:
    if row.ppid <= 0:
        return False
    parent = by_pid.get(row.ppid)
    if parent is None:
        # A census can omit a live protected parent; only a failed liveness probe confirms death.
        return not (primitives is not None and primitives.alive(row.ppid))
    return bool(row.ctime and parent.ctime and parent.ctime > row.ctime)


def select(census: Sequence, primitives: Optional[ReapPrimitives] = None) -> List:
    """grep/find rows of `census` whose parent is verified dead. A parent missing from the census
    is confirmed through `primitives.alive` before its child is selected."""
    if not census:
        return []
    by_pid = {r.pid: r for r in census}
    return [r for r in census if is_search_name(r.name) and _parent_dead(r, by_pid, primitives)]


def protected_pids(self_pid: Optional[int] = None) -> frozenset:
    """The current pid plus every ancestor. A kill path refuses these targets.

    An unreadable ancestry yields the pid alone."""
    me = os.getpid() if self_pid is None else self_pid
    pids = {me}
    try:
        import psutil

        pids.update(p.pid for p in psutil.Process(me).parents())
    except Exception:  # noqa: BLE001 -- ancestry is best-effort; own pid always protected
        pass
    return frozenset(pids)


def is_self_or_ancestor(pid: int, self_pid: Optional[int] = None) -> bool:
    return pid in protected_pids(self_pid)


def _terminate(pid: int) -> None:
    if is_self_or_ancestor(pid):
        return
    os.kill(pid, signal.SIGTERM)


def reap(primitives: ReapPrimitives, terminate: Callable[[int], None] = _terminate,
         self_pid: Optional[int] = None) -> List[int]:
    """Terminate each orphan `select` finds over a fresh census; return the PIDs reaped. A child
    whose creation time no longer matches its census row (PID reused since) is left alone."""
    census = primitives.snapshot()
    if not census:
        return []
    protected = protected_pids(self_pid)
    reaped: List[int] = []
    for row in select(census, primitives):
        if row.pid in protected:
            continue
        if row.ctime and primitives.creation_time(row.pid) != row.ctime:
            continue
        try:
            terminate(row.pid)
        except Exception:
            continue
        reaped.append(row.pid)
    return reaped
