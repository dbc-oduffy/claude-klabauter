"""Verified kill of a process tree plus a sweep for children spawned during the kill.

Shared by the TaskStop reaper and the watcher stand-down op. Every kill goes through a
KillPrimitives seam keyed on (pid, ctime); nothing here spawns or raises on its own.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from coordinator_core.bash_guards._taskstop_contract import (
    KillPrimitives,
    LaunchRecord,
    ProcRow,
    ReapOutcome,
    ReapRow,
)


def stale_children(rows: Iterable[ProcRow], killed: List[Tuple[int, int]], done: set) -> list:
    """Live processes whose ppid names a killed pid and that are no older than that parent
    (a younger-than-parent ctime is pid reuse, not a child)."""
    parent_ctime = dict(killed)
    return [
        r for r in rows
        if r.ppid in parent_ctime and (r.pid, r.ctime) not in done and r.ctime >= parent_ctime[r.ppid]
    ]


def kill_tree_verified(
    tree: Sequence[ProcRow],
    snapshot: Callable[[], Optional[Sequence[ProcRow]]],
    kill: KillPrimitives,
) -> Tuple[List[Tuple[int, int]], bool, Optional[str]]:
    """Terminate every process of tree, re-snapshot once and terminate the children the kill
    orphaned. Returns ([(pid, ctime) confirmed dead], complete, reason); complete is False when any
    terminate was refused or the re-snapshot was unreadable (the sweep could not run, so nothing
    is verified); reason names the unreadable case, else None."""
    killed: List[Tuple[int, int]] = []
    complete = True
    for p in tree:
        if kill.terminate_verified(p.pid, p.ctime):
            killed.append((p.pid, p.ctime))
        else:
            complete = False
    done = {(p.pid, p.ctime) for p in tree}
    again = snapshot()
    reason: Optional[str] = None
    if again:
        for r in stale_children(again, killed, done):
            if kill.terminate_verified(r.pid, r.ctime):
                killed.append((r.pid, r.ctime))
            else:
                complete = False
    else:
        complete = False
        reason = "re-snapshot unreadable; stale-child sweep not run"
    return killed, complete, reason


def reap_launch(
    rec: LaunchRecord,
    others: Sequence[LaunchRecord],
    snapshot: Callable[[], Optional[Sequence[ProcRow]]],
    kill: KillPrimitives,
) -> ReapRow:
    """Attribute rec to its one orphan tree and kill it verified; returns the row, never logs.
    NO_CANDIDATE on an empty snapshot; AMBIGUOUS/NO_CANDIDATE when attribution declines (nothing
    killed); else KILLED, or INCOMPLETE when any kill was refused or unverified."""
    rows = snapshot()
    if not rows:
        return ReapRow(rec.task_id, ReapOutcome.NO_CANDIDATE)
    # Lazy: watcher_stand_down imports this module and must not gain _session_census.
    from coordinator_core.bash_guards._taskstop_attribution import attribute

    tree, outcome, competing, candidates = attribute(rec, others, rows, kill.command_line)
    if tree is None:
        return ReapRow(rec.task_id, outcome, competing=competing, candidates=candidates)
    killed, complete, _ = kill_tree_verified(tree, lambda: snapshot(), kill)
    result = ReapOutcome.KILLED if complete else ReapOutcome.INCOMPLETE
    return ReapRow(rec.task_id, result, killed=tuple(killed), candidates=candidates)


__all__ = ["kill_tree_verified", "reap_launch", "stale_children"]
