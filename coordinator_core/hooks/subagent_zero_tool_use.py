"""
coordinator_core.hooks.subagent_zero_tool_use — SubagentStop bookkeeping op.

Purpose: Mechanical subagent tool-use counting, Stage 1 (write side). Counts
`tool_use` content blocks in the subagent's own transcript JSONL
(`agent_transcript_path`) and, ONLY on a verified count, appends one durable record to
this session's per-session store. This is the write/decision-logic half of a
cross-repo contract with DoE-claude — DoE owns the thin plumbing shim (hook
registration, the "has unsurfaced" sentinel, the surfaced-cursor) under the DR-047
transport-seam carve-out; this op owns the counting and the durable write.

Naming note (per DoE's 2026-07-25 finding, see
cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-store-records-every-count.md):
despite the `zero_tool_use` module/op name and the record's `kind: "zero-tool-use"`
field, this op writes ONE RECORD PER VERIFIED COUNT, not only zero. There is no
`if tool_use_count != 0` gate here, deliberately — see "Deliberately no zero-gate"
below. The name and `kind` identify the DETECTOR that produced the record (this
module owns exactly one kind, "zero-tool-use", per the future-multi-kind design
below), not a filter on the record's content. A verified zero is one value
`tool_use_count` can hold among many, not the thing that makes a record eligible to
be written. Any reader — this repo's or DoE's — that treats `kind == "zero-tool-use"`
as synonymous with "this agent did zero tool calls" will silently misreport every
healthy agent as a zero-tool-use detection; filter on the `tool_use_count` field
explicitly instead (see hooks.subagent_zero_tool_use_surface and
hooks.subagent_zero_tool_use_resolve, which do this).

The counting mechanism is spike-proven, not re-derived here: a deliberately-toolless
agent counted 0 `tool_use` blocks; a one-call agent counted 1 — both exactly reproduced
the harness notification's own `tool_uses` field. See
cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-detection-verdict-viable.md.

Negative-spec:
    Deliberately no zero-gate: do NOT add `if tool_use_count != 0` before the
    durable write. This store is a general per-session log of every verified
    tool-use count (see the Naming note above) — the full-count series supports
    "was this agent unusually quiet" / rate questions that a zero-only subset
    cannot, the data is already collected at zero marginal cost per this op's own
    counting pass, and discarding non-zero records to make the store's contents
    match its name is backwards: change the label, not the data. Callers that only
    want zero-count records filter at the read side (surface/resolve ops), not here.

    THE load-bearing rule of this op: an ABSENT or UNREADABLE transcript at
    `agent_transcript_path` MUST resolve to UNKNOWN and MUST NEVER be reported or
    recorded as a verified zero. 3 of 7 observed spike events had no transcript on
    disk at all — confirmed never written, not a flush race — making "no transcript"
    the COMMON case, not an edge case. On UNKNOWN this op writes NOTHING to the store
    and returns an envelope that cannot be confused with a verified 0.

    Do NOT register or handle a `TaskCompleted` case — DoE evaluated and rejected it
    (no `agent_id`, no transcript pointer in that payload); a handler here would be a
    dead trigger, never fired.

    Do NOT mark anything "surfaced" and do NOT perform "dispatched but no record
    arrived" reconciliation — both are deliberately DoE-side (their own per-session
    cursor and dispatch-tracking loop already own those concerns end-to-end; see the
    contract memo). This op's only job is: count, and durable-write on a verified
    count.

    Do NOT add atomicity/locking machinery to the durable write beyond a plain
    append — each session owns its own per-session store file; no cross-session or
    cross-process shared mutable state exists here to race.

Durable record schema (shared with hooks.subagent_zero_tool_use_surface, the read
side): one compact JSON object per line, fields in this exact order —
`kind` (literal "zero-tool-use", the discriminator), `session_id`, `agent_id`,
`agent_type`, `tool_use_count` (int, verified), `recorded_at` (ISO-8601 UTC,
Z-suffixed). `kind` is deliberate, not decorative: DR-084's claim primitive
(coordinator_core/session/claims.py) is handoff-frontmatter/mkdir-mutex shaped and is
explicitly negative-spec'd against hook-subprocess callers, so it cannot serve "return
unsurfaced records of kind K for session S" — this store is the general kind-
discriminated shape instead, built so a future detector can append records of a
different `kind` to the same per-session store and the surface op's filter still
works unmodified.

Store location: `<git_common_dir>/coordinator-sessions/<session_id>/subagent-zero-tool-use.jsonl`
— a sibling of the `dispatched-agents.txt` / `push-failures-cursor.txt` per-session
convention this tree already uses (track_dispatched_agents.py, auto_push.py).

Spec backlink: cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-detection-engine-op-contract.md
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir


#: Generator-provenance declaration: _append_record_sync writes to
#: <git_common_dir>/coordinator-sessions/<session_id>/
#: subagent-zero-tool-use.jsonl — inside .git/, never a tracked artifact.
GENERATES: list = []


def _count_tool_use_blocks(transcript_path: str) -> int | None:
    """Return the verified tool_use content-block count, or None on UNKNOWN.

    UNKNOWN covers: the file does not exist, is not a regular file, or cannot be
    opened/read for any OSError reason. A present-but-empty or all-malformed
    transcript is a VERIFIED count of 0, not UNKNOWN — the file itself was readable,
    so there is nothing ambiguous about a genuinely toolless run.

    Per-line tolerance: a line that is not valid JSON, or that does not parse to a
    JSON object, or whose "message"."content" is absent or not a list, is skipped
    (contributes 0 to the count) rather than aborting the whole count. A content
    block is counted when it is an element of that content list and is itself a
    dict with "type" == "tool_use".
    """
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                count += 1
    return count


def _append_record_sync(store_path: str, entry: dict) -> None:
    """Append one compact-JSON record line to the per-session store (blocking I/O).

    Called exclusively via asyncio.to_thread() — must not be awaited directly.
    Failures are swallowed: observability loss is preferable to crashing the engine
    on a non-fatal bookkeeping write (mirrors agent_completion_log._append_audit_entry).
    """
    store_dir = os.path.dirname(store_path)
    try:
        os.makedirs(store_dir, exist_ok=True)
    except OSError as exc:
        print(f"subagent_zero_tool_use: cannot create store dir {store_dir}: {exc}", file=sys.stderr)
        return
    try:
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with open(store_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"subagent_zero_tool_use: cannot write {store_path}: {exc}", file=sys.stderr)
        return


@register_op("hooks.subagent_zero_tool_use")
async def _handler(params: dict, repo_root=None) -> dict:
    """SubagentStop write op: count tool_use blocks, durable-write ONLY on a verified count.

    Side-effect (verified count only): appends one line to
    .git/coordinator-sessions/<session_id>/subagent-zero-tool-use.jsonl.

    On UNKNOWN (absent/unreadable transcript, or absent repo_root/session_id):
    writes nothing, returns no_advisory() — never a count, never confusable with a
    verified zero.

    Inputs (flat scalar, extracted via _payload.field(); "" treated as absent):
        session_id, agent_id, agent_type, agent_transcript_path, hook_event_name.
    """
    import asyncio

    session_id = field(params, "session_id")
    agent_id = field(params, "agent_id")
    agent_type = field(params, "agent_type")
    transcript_path = field(params, "agent_transcript_path")

    # No repo to write into, or no session to key the store by — UNKNOWN, no write.
    if not repo_root or not session_id or not transcript_path:
        return no_advisory()

    tool_use_count = await asyncio.to_thread(_count_tool_use_blocks, transcript_path)

    # THE load-bearing branch: absent/unreadable transcript is UNKNOWN, never zero.
    # Write nothing; return the same clean no-op envelope used on every other
    # UNKNOWN path so this case is never confusable with a verified count.
    if tool_use_count is None:
        return no_advisory()

    try:
        _sessions_base = git_common_dir(repo_root) / "coordinator-sessions"
    except RuntimeError:
        _sessions_base = Path(str(repo_root)) / "coordinator-sessions"
    store_path = str(_sessions_base / session_id / "subagent-zero-tool-use.jsonl")

    recorded_at = (
        datetime.datetime.now(tz=datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    entry = {
        "kind": "zero-tool-use",
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "tool_use_count": tool_use_count,
        "recorded_at": recorded_at,
    }

    await asyncio.to_thread(_append_record_sync, store_path, entry)

    return no_advisory()
