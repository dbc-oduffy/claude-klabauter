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


_BOUNDARY = None


def _events(tail: str) -> list:
    """Ordered transcript events in `tail`: an assistant `tool_use` block yields its name, a
    compaction boundary yields `_BOUNDARY`. Real transcripts are JSONL envelopes
    (`{"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}`); a boundary is
    `{"type":"system","subtype":"compact_boundary"}` or the summary entry carrying
    `isCompactSummary`. Malformed lines (incl. the tail's cut first line) are skipped."""
    events: list = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("isCompactSummary") is True or (
            entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"
        ):
            events.append(_BOUNDARY)
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name:
                    events.append(name)
    return events


def _previous_tool_use_name(events: list) -> Optional[str]:
    """The last `tool_use` name before this call (PreToolUse: the current call is not yet
    recorded, so the most recent record is a prior call)."""
    names = [e for e in events if e is not _BOUNDARY]
    return names[-1] if names else None


def _compaction_boundary_is_most_recent_event(events: list) -> bool:
    """True iff at most one `tool_use` has landed since the LAST compaction boundary — the
    "first status read right after a compaction boundary" grace, not an indefinite one. That
    one allowed entry is the PRIOR poll call itself."""
    last = -1
    for i, e in enumerate(events):
        if e is _BOUNDARY:
            last = i
    if last < 0:
        return False
    return sum(1 for e in events[last + 1:] if e is not _BOUNDARY) <= 1


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
        events = _events(tail)
        consecutive = (
            not _compaction_boundary_is_most_recent_event(events)
            and _previous_tool_use_name(events) in POLL_TOOLS
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
