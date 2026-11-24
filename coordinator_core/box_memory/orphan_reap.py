"""Reap grep/find processes whose parent is verified dead and which are at least 30 minutes old.

Dead parent: the ppid is absent from the census and fails a liveness probe, or present with a creation time later than the
child's (the PID was reused). An unreadable census, a zero ctime or a ppid of 0 reads as alive; a zero ctime also reads as
young. A dead parent alone proves nothing: Git Bash fork emulation drops the parent of a live pipeline.
No per-process spawn: one census read, then a direct terminate per orphan.
"""

from __future__ import annotations

import os
import signal
from typing import Any, Callable, List, Optional, Sequence

from coordinator_core.box_memory import ReapPrimitives
from coordinator_core.box_memory.pressure import (
    MIN_ORPHAN_CPU_S,
    is_aged,
    is_aged_orphan,
    is_python_name,
    is_stdin_python,
)


def select(census: Sequence, primitives: Optional[ReapPrimitives] = None,
           now: Optional[int] = None) -> List:
    """grep/find rows of `census` whose parent is verified dead AND whose age is at least
    `pressure.MIN_ORPHAN_AGE_S` (`now` in ProcRow.ctime units, default the host clock). A parent
    missing from the census is confirmed through `primitives.creation_time` before its child is selected."""
    if not census:
        return []
    by_pid = {r.pid: r for r in census}
    return [r for r in census if is_aged_orphan(r, by_pid, primitives, now)]


def _stdin_python_reads(census: Sequence, primitives: ReapPrimitives,
                        now: Optional[int] = None) -> List[tuple]:
    """`(row, cpu_s, command_line)` per hit, so a caller reuses the reads that selected it."""
    out: List[tuple] = []
    for row in census or ():
        if not is_python_name(row.name) or not is_aged(row, now):
            continue
        cpu = primitives.cpu_time_s(row.pid)
        if cpu is None or cpu < MIN_ORPHAN_CPU_S:
            continue
        cmd = primitives.command_line(row.pid)
        if is_stdin_python(row, cmd):
            out.append((row, cpu, cmd))
    return out


def select_stdin_python(census: Sequence, primitives: ReapPrimitives,
                        now: Optional[int] = None) -> List:
    """Python rows reading their script from stdin (`python -`), aged at least `MIN_ORPHAN_AGE_S`, with
    CPU time at least `MIN_ORPHAN_CPU_S`. Cheap gates run first: `command_line` is read only for python
    rows past the age and CPU gates; an unreadable CPU time or command line is not selected.
    Neither a dead parent nor a missing claude ancestor gates: a live call looks the same."""
    return [row for row, _, _ in _stdin_python_reads(census, primitives, now)]


def _claude_ancestor(row, by_pid: dict) -> Any:
    """The nearest claude-named ancestor reached by ppid, or None (broken chain, reused pid, cycle)."""
    from coordinator_core.bash_guards._session_census import _is_claude

    node = row
    for _ in range(64):
        parent = by_pid.get(node.ppid)
        if parent is None or parent.pid == node.pid or parent.ctime > node.ctime:
            return None
        if _is_claude(parent.name):
            return parent
        node = parent
    return None


def reap_stdin_python(census: Sequence, primitives: ReapPrimitives,
                      now: Optional[int] = None) -> List[dict]:
    """Flag each `select_stdin_python` hit over the caller's `census` (never a second table read).

    Returns `{pid, ctime, cmdline, cpu_s, session, action}` per hit, `action` always "flagged": a
    heredoc python of an ended tool call is indistinguishable from a live background one, so nothing
    is terminated and nothing is sent (reporting is the caller's). `session` is `{claude_pid,
    claude_ctime}` when the ppid chain reaches a claude, else None."""
    by_pid = {r.pid: r for r in census or ()}
    hits: List[dict] = []
    for row, cpu, cmd in _stdin_python_reads(census, primitives, now):
        claude = _claude_ancestor(row, by_pid)
        hits.append({
            "pid": row.pid,
            "ctime": row.ctime,
            "cmdline": cmd,
            "cpu_s": cpu,
            "session": {"claude_pid": claude.pid, "claude_ctime": claude.ctime} if claude else None,
            "action": "flagged",
        })
    return hits


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
