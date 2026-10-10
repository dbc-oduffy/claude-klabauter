"""coordinator_core.hooks.taskstop_reaper -- PreToolUse(TaskStop) orphan-tree reaper.

Attributes a TaskStop to the one orphan process tree its recorded background launch left and
kills that tree, every kill verified by (pid, ctime). Always allows the TaskStop; any doubt or
failure means no kill plus a log row. Windows only: off Windows no record exists and it is a no-op.
"""

from __future__ import annotations

import sys
from typing import List, Tuple

from coordinator_core.bash_guards import _host_probe as _hp
from coordinator_core.bash_guards import _process_kill
from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_attribution import attribute
from coordinator_core.bash_guards._taskstop_contract import REAPER_OP, ReapOutcome, ReapRow
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op

# Seams: tests substitute a KillPrimitives and a snapshot callable.
_kill = _process_kill
_snapshot = _hp.snapshot


def _log(row: ReapRow) -> None:
    try:
        store.append_row(row)
    except Exception:
        pass


def _stale_children(rows, killed: List[Tuple[int, int]], done: set) -> list:
    """Live processes whose ppid names a killed pid and that are no older than that parent
    (a younger-than-parent ctime is pid reuse, not a child)."""
    parent_ctime = dict(killed)
    return [
        r for r in rows
        if r.ppid in parent_ctime and (r.pid, r.ctime) not in done and r.ctime >= parent_ctime[r.ppid]
    ]


def _reap(payload: dict) -> None:
    tool_input = payload.get("tool_input")
    task_id = tool_input.get("task_id") if isinstance(tool_input, dict) else None
    if not store.valid_task_id(task_id):
        return
    rec = store.read_record(task_id)
    if rec is None:
        _log(ReapRow(task_id, ReapOutcome.NO_RECORD))
        return
    if rec.session_id != payload.get("session_id"):
        _log(ReapRow(task_id, ReapOutcome.SESSION_MISMATCH))
        return
    others = store.all_records()
    rows = _snapshot()
    if not rows:
        _log(ReapRow(task_id, ReapOutcome.NO_CANDIDATE))
        return
    tree, outcome, competing, candidates = attribute(rec, others, rows, _kill.command_line)
    if tree is None:
        _log(ReapRow(task_id, outcome, competing=competing, candidates=candidates))
        return

    killed: List[Tuple[int, int]] = []
    complete = True
    for p in tree:
        if _kill.terminate_verified(p.pid, p.ctime):
            killed.append((p.pid, p.ctime))
        else:
            complete = False
    done = {(p.pid, p.ctime) for p in tree}
    again = _snapshot()
    if again:
        for r in _stale_children(again, killed, done):
            if _kill.terminate_verified(r.pid, r.ctime):
                killed.append((r.pid, r.ctime))
            else:
                complete = False
    result = ReapOutcome.KILLED if complete else ReapOutcome.INCOMPLETE
    _log(ReapRow(task_id, result, killed=tuple(killed), candidates=candidates))
    store.delete_record(task_id)


@register_op(REAPER_OP)
def _handler(params: dict, repo_root=None) -> dict:
    """Reap the orphan tree of a TaskStop's background launch; always returns no_advisory()."""
    try:
        if sys.platform == "win32":
            payload = payload_of(params)
            if payload.get("tool_name") == "TaskStop":
                _reap(payload)
    except Exception:
        pass
    return no_advisory()
