"""Verified kill of a process tree plus a sweep for children spawned during the kill.

Shared by the TaskStop reaper and the watcher stand-down op. Every kill goes through a
KillPrimitives seam keyed on (pid, ctime); nothing here spawns or raises on its own.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from coordinator_core.bash_guards._taskstop_contract import KillPrimitives, ProcRow


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


__all__ = ["kill_tree_verified", "stale_children"]
