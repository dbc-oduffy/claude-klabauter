"""coordinator_core.hooks.taskstop_launch_recorder -- PostToolUse(Bash) launch recorder.

Notes each Windows background Bash launch as backgroundTaskId -> (session, tool_use_id, command,
launch mark) in the box-wide store, so the PreToolUse(TaskStop) reaper can find the orphan tree
that launch left. Writes only; never denies, never raises, spawns nothing. Off Windows it is a
no-op.
"""

from __future__ import annotations

import sys
import time

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_contract import (
    FILETIME_UNIX_EPOCH,
    RECORDER_OP,
    LaunchRecord,
)
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op


def _filetime_now() -> int:
    return int(time.time() * 1e7) + FILETIME_UNIX_EPOCH


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


@register_op(RECORDER_OP)
def _handler(params: dict, repo_root=None) -> dict:
    """Record a background Bash launch; always returns no_advisory()."""
    mark = _filetime_now()
    try:
        if sys.platform != "win32":
            return no_advisory()
        payload = payload_of(params)
        if payload.get("tool_name") != "Bash":
            return no_advisory()
        tool_input = payload.get("tool_input")
        tool_response = payload.get("tool_response")
        if not isinstance(tool_input, dict) or not isinstance(tool_response, dict):
            return no_advisory()
        if tool_input.get("run_in_background") is not True:
            return no_advisory()
        task_id = tool_response.get("backgroundTaskId")
        if not store.valid_task_id(task_id):
            return no_advisory()
        store.write_record(
            LaunchRecord(
                task_id=task_id,
                session_id=_str(payload.get("session_id")),
                tool_use_id=_str(payload.get("tool_use_id")),
                command=_str(tool_input.get("command")),
                mark=mark,
            )
        )
    except Exception:
        pass
    return no_advisory()
