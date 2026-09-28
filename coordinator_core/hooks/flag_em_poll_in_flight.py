"""
coordinator_core.hooks.flag_em_poll_in_flight — PreToolUse(TaskList|TaskGet|TaskOutput|
ListAgents|ReadNotifications) advisory op.

Purpose: docs/plans/2026-09-27-four-turn-em-loop.md § Settled here 4 / § Pinned interfaces
(task C17). Flags the EM spending a turn checking on work that a Workflow completion
notification would wake it for anyway. Fires on the SECOND CONSECUTIVE poll-tool call with
no intervening non-poll tool call, while the session has a fired Workflow with no observed
completion. Always allows — advisory only, never a deny.

In-flight source: the Workflow run record `_capture_workflow_run_record_sync`
(`coordinator_core.hooks.postuse_advisory_dispatch`) already persists under
`tempfile.gettempdir()/workflow-run-<session>-<task>.json`. This op adds no second marker
and does not edit that file — it only reads whether a record for this session exists and
whether the transcript tail carries that run's completion.

Consecutiveness source: the last 64 KiB of `payload["transcript_path"]`, never a second
registration — the op is registered on the poll tools only (§ Pinned interfaces "Poll op"),
so it has to look BACKWARD in the transcript to see whether a non-poll tool call (Bash, Read,
Edit, ...) intervened since the previous poll.

Exemptions: a dispatched subagent's own calls (`payload["agent_id"]` present — its poll is not
the EM's own halting decision), and the first status read right after a compaction boundary
(compaction re-orientation is a legitimate single status read, § Settled here 4).

Per-session flag count is persisted the same way `postuse_advisory_dispatch` persists its own
durable per-session state (`tempfile.gettempdir()/em-poll-count-<session>.json`, key `flags`) —
the metric the slate run (V1) reads. Written only on a flag.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Mapping, Optional

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import allow_advisory, no_advisory, payload_of

#: Registered matcher names (C18 narrows hooks.json's HTTP matcher to the same set).
POLL_TOOLS = frozenset(
    {"TaskList", "TaskGet", "TaskOutput", "ListAgents", "ReadNotifications"}
)

_TAIL_BYTES = 64 * 1024

_ANCHOR = (
    "coordinator/docs/wiki/coordinator-tripwires/tripwire-registry/"
    "em-poll-while-a-workflow-runs-is-flagged.md"
)

# Best-effort assistant tool_use name capture, oldest-to-newest as they appear in the raw
# transcript text. Defensive like `_capture_script_path_and_args`'s regex scan in
# postuse_advisory_dispatch.py — a miss here degrades to "no prior tool seen", never a
# false flag, because the caller only flags on an EXPLICIT prior poll-tool match.
_TOOL_USE_NAME_RE = re.compile(r'"type"\s*:\s*"tool_use"[^{}]*?"name"\s*:\s*"(?P<name>[A-Za-z0-9_]+)"')

# A defensive, best-effort compact-boundary marker. Claude Code transcripts carry a system
# entry naming the compaction; several shapes have been observed across harness versions, so
# this matches loosely rather than pinning one JSON shape (same posture as the tool_use scan
# above — a miss here is a false-negative on the exemption, not a false flag).
_COMPACT_BOUNDARY_RE = re.compile(r'"isCompactSummary"\s*:\s*true|"subtype"\s*:\s*"compact_boundary"')

_RUN_ID_RE = re.compile(r'"run_id"\s*:\s*"(?P<run_id>[A-Za-z0-9_-]+)"')


def _tmpdir() -> str:
    return tempfile.gettempdir()


def _poll_state_path(tmpdir: str, session_id: str) -> str:
    return os.path.join(tmpdir, f"em-poll-count-{session_id}.json")


def _load_poll_state(tmpdir: str, session_id: str) -> dict:
    try:
        with open(_poll_state_path(tmpdir, session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_poll_state(tmpdir: str, session_id: str, state: dict) -> None:
    path = _poll_state_path(tmpdir, session_id)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=tmpdir, prefix=".em-poll-count-", suffix=".tmp")
    except Exception:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _read_tail(path: str, n: int = _TAIL_BYTES) -> Optional[str]:
    """The trailing `n` bytes of the transcript, decoded leniently. None on any I/O error —
    the failing-open case names a missing/unreadable transcript, which fails the whole check
    silently (module docstring: "a missing or unreadable transcript fails open silently")."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > n:
                fh.seek(size - n)
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _previous_tool_use_name(tail: str) -> Optional[str]:
    """The last `tool_use` name in the tail BEFORE this call. `finditer` walks the whole tail
    in file order, so the last match is the most recent tool_use record — which, on a
    PreToolUse hook, is necessarily a PRIOR call (the current one has not been recorded yet)."""
    names = [m.group("name") for m in _TOOL_USE_NAME_RE.finditer(tail)]
    return names[-1] if names else None


def _compaction_boundary_is_most_recent_event(tail: str) -> bool:
    """True iff at most one `tool_use` record has landed since the LAST compaction-boundary
    marker in `tail` — the "first status read right after a compaction boundary" grace
    (module docstring), not an indefinite one. The one recorded entry allowed to sit after
    the boundary is the PRIOR poll call itself (`_previous_tool_use_name`'s own read); this
    call is checked against that same entry for consecutiveness, so exemption looks at
    whether the boundary is more recent than the tool_use call BEFORE that one.

    `.search()` alone (the prior bug) exempted every later poll for as long as ANY
    compaction marker stayed anywhere in the 64 KiB tail, however many intervening tool
    calls — poll or not — followed it."""
    boundary_positions = [m.start() for m in _COMPACT_BOUNDARY_RE.finditer(tail)]
    if not boundary_positions:
        return False
    last_boundary = boundary_positions[-1]
    tool_positions = [m.start() for m in _TOOL_USE_NAME_RE.finditer(tail)]
    cutoff = tool_positions[-2] if len(tool_positions) >= 2 else -1
    return last_boundary > cutoff


def _run_ids_in_flight(tmpdir: str, session_id: str) -> list:
    """`run_id`s this session's own workflow-run records name, read off
    `workflow-run-<session_id>-<task_id>.json` (`_capture_workflow_run_record_sync`'s own
    persist shape). Best-effort glob; any I/O error yields an empty list (no in-flight run
    seen), which is the fail-open direction — never claim a run is in flight on a read error."""
    try:
        prefix = f"workflow-run-{session_id}-"
        ids = []
        for name in os.listdir(tmpdir):
            if not (name.startswith(prefix) and name.endswith(".json")):
                continue
            try:
                with open(os.path.join(tmpdir, name), "r", encoding="utf-8") as fh:
                    record = json.load(fh)
                run_id = record.get("run_id") if isinstance(record, dict) else None
                if isinstance(run_id, str) and run_id:
                    ids.append(run_id)
            except Exception:
                continue
        return ids
    except Exception:
        return []


def _run_completed_in_tail(tail: str, run_ids: list) -> bool:
    """True when the tail names one of `run_ids`' completion — a task notification carrying
    the run id. Best-effort substring match: the exact notification shape is harness-owned and
    not pinned here, so this looks for the run id appearing anywhere in the tail alongside
    completion-shaped text, and treats a bare co-occurrence as completion (fail toward NOT
    flagging, since a completed run wrongly read as in-flight is the safer direction — the
    session is at most one unwarranted advisory line away from correct, never blocked)."""
    for run_id in run_ids:
        if run_id in tail:
            window_start = max(0, tail.find(run_id) - 200)
            window = tail[window_start: tail.find(run_id) + 200]
            if re.search(r"completed|idle|finished|landing", window, re.IGNORECASE):
                return True
    return False


@register_op("hooks.flag_em_poll_in_flight")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(poll tool) advisory: flag a second consecutive EM poll call while a
    dispatched Workflow is in flight and no intervening non-poll tool call was made.

    Always returns allow (D2 shape a on a flag, no_advisory — shape c — otherwise). Never
    denies, never raises: every internal check is wrapped to fail toward NOT flagging.
    """
    payload = payload_of(params)

    # A dispatched subagent's own poll is not the EM's own halting decision.
    if payload.get("agent_id"):
        return no_advisory()

    tool_name = payload.get("tool_name")
    if tool_name not in POLL_TOOLS:
        return no_advisory()

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()

    transcript_path = payload.get("transcript_path") or ""
    if not isinstance(transcript_path, str) or not transcript_path:
        return no_advisory()

    tail = _read_tail(transcript_path)
    if tail is None:
        return no_advisory()

    # Consecutive iff the most recent prior tool_use is itself a poll and no compaction
    # boundary sits in the tail — the transcript is the whole state; nothing is carried.
    try:
        consecutive = (
            not _compaction_boundary_is_most_recent_event(tail)
            and _previous_tool_use_name(tail) in POLL_TOOLS
        )
    except Exception:
        consecutive = False

    flagged = False
    if consecutive:
        tmpdir = _tmpdir()
        try:
            run_ids = _run_ids_in_flight(tmpdir, session_id)
            flagged = bool(run_ids) and not _run_completed_in_tail(tail, run_ids)
        except Exception:
            flagged = False
        if flagged:
            state = _load_poll_state(tmpdir, session_id)
            _save_poll_state(tmpdir, session_id, {"flags": int(state.get("flags") or 0) + 1})

    if not flagged:
        return no_advisory()

    return allow_advisory(
        "PreToolUse",
        "EM poll while workflow runs — the completion notification wakes you; end the turn. "
        f"See {_ANCHOR}.",
    )
