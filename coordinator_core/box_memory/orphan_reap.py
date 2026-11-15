"""Reap grep/find processes whose parent is verified dead and which are at least 30 minutes old.

Dead parent: the ppid is absent from the census and fails a liveness probe, or present with a creation time later than the
child's (the PID was reused). An unreadable census, a zero ctime or a ppid of 0 reads as alive; a zero ctime also reads as
young. A dead parent alone proves nothing: Git Bash fork emulation drops the parent of a live pipeline.
No per-process spawn: one census read, then a direct terminate per orphan.
"""

from __future__ import annotations

import os
import signal
from typing import Callable, List, Optional, Sequence

from coordinator_core.box_memory import ReapPrimitives
from coordinator_core.box_memory.pressure import is_aged_orphan


def select(census: Sequence, primitives: Optional[ReapPrimitives] = None,
           now: Optional[int] = None) -> List:
    """grep/find rows of `census` whose parent is verified dead AND whose age is at least
    `pressure.MIN_ORPHAN_AGE_S` (`now` in ProcRow.ctime units, default the host clock). A parent
    missing from the census is confirmed through `primitives.creation_time` before its child is selected."""
    if not census:
        return []
    by_pid = {r.pid: r for r in census}
    return [r for r in census if is_aged_orphan(r, by_pid, primitives, now)]


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
         self_pid: Optional[int] = None, now: Optional[int] = None) -> List[int]:
    """Terminate each aged orphan `select` finds over a fresh census; return the PIDs reaped. A
    child whose creation time no longer matches its census row (PID reused since) is left alone."""
    census = primitives.snapshot()
    if not census:
        return []
    protected = protected_pids(self_pid)
    reaped: List[int] = []
    for row in select(census, primitives, now):
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
