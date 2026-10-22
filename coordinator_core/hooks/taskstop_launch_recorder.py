"""coordinator_core.hooks.taskstop_launch_recorder -- PostToolUse(Bash) launch recorder.

Notes each Windows background Bash launch, and each call the harness auto-backgrounded on timeout
(timedOutAfterMs), as backgroundTaskId -> (session, tool_use_id, command, launch mark) in the
box-wide store, so the PreToolUse(TaskStop) reaper can find the orphan tree that launch left. A
timed-out read-only search is reaped at the instant; every other call is only recorded. Never
denies, never raises. Off Windows it is a no-op.
"""

from __future__ import annotations

import sys
import time

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_contract import (
    FILETIME_UNIX_EPOCH,
    LaunchRecord,
)
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op


def _filetime_now() -> int:
    return int(time.time() * 1e7) + FILETIME_UNIX_EPOCH


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


_SEARCH_HEADS = {"grep", "egrep", "fgrep", "rg", "find", "select-string"}
_FIND_WRITERS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls")

# Seams: tests substitute a snapshot callable and a KillPrimitives; None means the real ones.
_snapshot = None
_kill = None


def _timed_out(tool_input: dict, tool_response: dict) -> bool:
    """True for a harness auto-backgrounded call: positive-int timedOutAfterMs, no
    run_in_background request, valid backgroundTaskId."""
    ms = tool_response.get("timedOutAfterMs")
    return (
        isinstance(ms, int)
        and not isinstance(ms, bool)
        and ms > 0
        and tool_input.get("run_in_background") is not True
        and store.valid_task_id(tool_response.get("backgroundTaskId"))
    )


def _read_only_search(command: str) -> bool:
    """True only when every segment of command is a read-only search (grep, rg, find,
    Select-String). Any doubt, redirect, substitution or empty command is False."""
    if not isinstance(command, str) or ">" in command or "$(" in command or "`" in command:
        return False
    from coordinator_core.bash_guards._dc_shell_text import _split_segments

    segs = [s.split() for s in _split_segments(command)]
    segs = [s for s in segs if s]
    if segs and segs[0][0] == "cd":
        segs = segs[1:]
    if not segs:
        return False
    for toks in segs:
        head = toks[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if head.endswith(".exe"):
            head = head[:-4]
        if head not in _SEARCH_HEADS:
            return False
        if head == "find" and any(
            t in _FIND_WRITERS or t.startswith("-fprint") for t in toks[1:]
        ):
            return False
    return True


def _reap_or_record(rec: LaunchRecord) -> None:
    """Reap a timed-out read-only search now; keep the record unless the tree is gone."""
    from coordinator_core.bash_guards import _host_probe, _process_kill
    from coordinator_core.bash_guards._taskstop_contract import ReapOutcome
    from coordinator_core.bash_guards._taskstop_kill import reap_launch

    snapshot = _snapshot or _host_probe.snapshot
    kill = _kill or _process_kill
    row = reap_launch(rec, store.all_records(), snapshot, kill)
    store.append_row(row)
    if row.outcome in (ReapOutcome.NO_CANDIDATE, ReapOutcome.AMBIGUOUS):
        store.write_record(rec)


@register_op("hooks.taskstop_launch_recorder")
def _handler(params: dict, repo_root=None) -> dict:
    """Record a background Bash launch or a timed-out call, and reap a timed-out read-only
    search at once; always returns no_advisory()."""
    payload = payload_of(params)
    mark = _filetime_now()
    try:
        if sys.platform != "win32":
            return no_advisory()
        tool_name = payload.get("tool_name")
        if tool_name not in ("Bash", "PowerShell"):
            return no_advisory()
        tool_input = payload.get("tool_input")
        tool_response = payload.get("tool_response")
        if not isinstance(tool_input, dict) or not isinstance(tool_response, dict):
            return no_advisory()
        if tool_input.get("run_in_background") is not True:
            if _timed_out(tool_input, tool_response):
                command = _str(tool_input.get("command"))
                rec = LaunchRecord(
                    task_id=tool_response["backgroundTaskId"],
                    session_id=_str(payload.get("session_id")),
                    tool_use_id=_str(payload.get("tool_use_id")),
                    command=command,
                    mark=mark - tool_response["timedOutAfterMs"] * 10_000,
                )
                if _read_only_search(command):
                    _reap_or_record(rec)
                else:
                    store.write_record(rec)
            return no_advisory()
        if tool_name != "Bash":
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
