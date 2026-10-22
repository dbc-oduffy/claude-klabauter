"""coordinator_core.hooks.taskstop_reaper -- PreToolUse(TaskStop) orphan-tree reaper.

Attributes a TaskStop to the one orphan process tree its recorded background launch left and
kills that tree, every kill verified by (pid, ctime). Always allows the TaskStop; any doubt or
failure means no kill plus a log row. Windows only: off Windows no record exists and it is a no-op.
"""

from __future__ import annotations

import sys

from coordinator_core.bash_guards import _host_probe as _hp
from coordinator_core.bash_guards import _process_kill
from coordinator_core.bash_guards._taskstop_kill import reap_launch
from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_contract import ReapOutcome, ReapRow
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op
from coordinator_core.temp_layout import managed_tempdir
from coordinator_core.workflow_watch.stall import stopped_marker_path

# Seams: tests substitute a KillPrimitives and a snapshot callable.
_kill = _process_kill
_snapshot = _hp.snapshot


def _log(row: ReapRow) -> None:
    try:
        store.append_row(row)
    except Exception:
        pass


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
    row = reap_launch(rec, store.all_records(), _snapshot, _kill)
    _log(row)
    if row.outcome in (ReapOutcome.KILLED, ReapOutcome.INCOMPLETE):
        store.delete_record(task_id)


def _write_stopped_marker(payload: dict) -> None:
    """Touch the stopped marker for this TaskStop so the stall detector never offers the run."""
    tool_input = payload.get("tool_input")
    task_id = tool_input.get("task_id") if isinstance(tool_input, dict) else None
    path = stopped_marker_path(str(managed_tempdir()), payload.get("session_id"), task_id)
    with open(path, "a", encoding="utf-8"):
        pass


@register_op("hooks.taskstop_reaper")
def _handler(params: dict, repo_root=None) -> dict:
    """Mark a TaskStop's task stopped, then reap the orphan tree of its background launch;
    always returns no_advisory()."""
    payload = payload_of(params)
    try:
        if payload.get("tool_name") == "TaskStop":
            _write_stopped_marker(payload)
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            if payload.get("tool_name") == "TaskStop":
                _reap(payload)
    except Exception:
        pass
    return no_advisory()
