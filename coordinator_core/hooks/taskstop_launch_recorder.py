"""coordinator_core.hooks.taskstop_launch_recorder -- PostToolUse(Bash) launch recorder.

Notes each Windows background Bash launch, and each call the harness auto-backgrounded on timeout
(timedOutAfterMs), as backgroundTaskId -> (session, tool_use_id, command, launch mark) in the
box-wide store, so the PreToolUse(TaskStop) reaper can find the orphan tree that launch left. A
timed-out read-only search is reaped at the instant; every other call is only recorded. Never
denies, never raises. Off Windows it is a no-op. A literal detached launch (nohup, trailing &,
Start-Process) or any run_in_background launch also leaves one session-stamped spawn-ledger row per
corroborated pid.
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
    row = reap_launch(rec, store.records_near(rec.mark), snapshot, kill)
    store.append_row(row)
    if row.outcome is not ReapOutcome.KILLED:
        store.write_record(rec)


def _create_time(pid: int) -> "float | None":
    """psutil create_time() (float epoch seconds), the value the stop side compares by equality;
    None for a vanished pid. Never a tick conversion of ProcRow.ctime."""
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:
        return None


def _detached_needles(command: str, run_in_background: bool = False) -> list:
    """Argv needles (stem-normalised) of each detached stage: the stage itself (matches a nohup.exe
    wrapper) and its peeled form (matches the wrapped child). A run_in_background launch detaches
    every stage."""
    from coordinator_core.bash_guards._command_tokenizer import (
        _peel_command_position,
        tokenize_full_command,
    )
    from coordinator_core.bash_guards._taskstop_attribution import _argv
    from coordinator_core.ops import _spawn_ledger as ledger

    tokens = tokenize_full_command(command)
    if tokens is None:
        return []
    stages = ledger._stages_with_separator(tokens)
    out = []
    for i, (stage, sep) in enumerate(stages):
        if stage[0].rsplit("/", 1)[-1] == "disown":
            picked = [stages[i - 1][0]] if i > 0 else []
        elif run_in_background or ledger._is_detaching_stage(stage, sep):
            picked = [stage]
        else:
            continue
        for st in picked:
            peeled, _conf = _peel_command_position(st)
            for needle in (st, peeled):
                if needle:
                    out.append(_argv(" ".join(needle)))
    return [n for n in out if n]


def _capture_ledger(payload: dict, tool_input: dict, tool_response: dict, mark: int) -> None:
    """Ledger every pid a literal detached launch left, stamped with the payload's session and agent.

    TRAP: a subagent's PostToolUse payload carries the EM's session_id with agent_id set, so the
    row's session is the EM's and the EM can stop its subagent's pid. One snapshot, taken only for
    a "recordable" shape. An ambiguous or unreadable window writes nothing; never raises.
    """
    from coordinator_core.bash_guards import _host_probe, _process_kill
    from coordinator_core.bash_guards._session_census import (
        _children,
        _is_claude,
        _is_orphan_root,
        _subtree,
    )
    from coordinator_core.bash_guards._taskstop_attribution import (
        _PRE_TICKS,
        _TICKS_PER_S,
        _argv,
        _contiguous,
        _window,
    )
    from coordinator_core.ops import _spawn_ledger as ledger

    command = _str(tool_input.get("command"))
    session_id = _str(payload.get("session_id"))
    in_bg = tool_input.get("run_in_background") is True
    if (
        not ledger.valid_session_id(session_id)
        or ledger.detach_shape(command, run_in_background=in_bg) != "recordable"
    ):
        return
    needles = _detached_needles(command, in_bg)
    if not needles:
        return
    lo, hi = _window(mark)
    ms = tool_response.get("durationMs")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
        lo = min(lo, mark - int(ms * _TICKS_PER_S / 1000) - _PRE_TICKS)
    rows = (_snapshot or _host_probe.snapshot)()
    if not rows:
        return
    command_line = (_kill or _process_kill).command_line
    by_pid = {r.pid: r for r in rows}
    children = _children(rows)
    matches = []
    for r in rows:
        if not (lo <= r.ctime <= hi):
            continue
        if in_bg:
            # The background shell stays under claude.exe: take the top of each in-window chain.
            parent = by_pid.get(r.ppid)
            if _is_claude(r.name) or (
                parent is not None
                and parent.ctime <= r.ctime
                and lo <= parent.ctime <= hi
                and not _is_claude(parent.name)
            ):
                continue
        elif not _is_orphan_root(r, by_pid):
            continue
        hits = []
        for p in _subtree(r, children):
            text = command_line(p.pid, p.ctime)
            if text is None:
                return
            argv = _argv(text)
            if any(_contiguous(n, argv) for n in needles):
                hits.append((p, text))
        if hits:
            matches.append(hits)
    if len(matches) != 1:
        return
    agent_id = _str(payload.get("agent_id"))
    for p, text in matches[0]:
        created = _create_time(p.pid)
        if created is not None:
            ledger.write_row(ledger.LedgerRow(p.pid, created, session_id, agent_id, _argv(text)))


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
        try:
            _capture_ledger(payload, tool_input, tool_response, mark)
        except Exception:
            pass
        if tool_input.get("run_in_background") is not True:
            if _timed_out(tool_input, tool_response):
                command = _str(tool_input.get("command"))
                rec = LaunchRecord(
                    task_id=tool_response["backgroundTaskId"],
                    session_id=_str(payload.get("session_id")),
                    tool_use_id=_str(payload.get("tool_use_id")),
                    command=command,
                    mark=mark - tool_response["timedOutAfterMs"] * 10_000,
                    agent_id=_str(payload.get("agent_id")),
                    timed_out=True,
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
                agent_id=_str(payload.get("agent_id")),
            )
        )
    except Exception:
        pass
    return no_advisory()
