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
from coordinator_core.lifecycle import git_common_dir, main_worktree_root


#: Generator-provenance declaration: _append_record_sync writes to
#: <git_common_dir>/coordinator-sessions/<session_id>/
#: subagent-zero-tool-use.jsonl — inside .git/, never a tracked artifact.
GENERATES: list = []


def _last_assistant_text(transcript_path: str) -> str:
    """Return the text of the finishing agent's LAST assistant message, or "".

    Companion read to `_count_tool_use_blocks`, kept a separate pass rather than
    folded into it: the two want opposite scan directions (forward count vs.
    reverse "last one wins"), and a shared single-pass implementation would
    have to buffer the whole file to find the tail regardless. Tail-reads only
    the last 512KB — the tail-seek discipline of
    `hooks.nudge_harness_directive_dispatch.last_assistant_text`, not re-
    derived: this hook is on the same hot Stop-adjacent path, and a
    megabyte-scale transcript must not be read in full just to find its last
    line.

    Failure modes all resolve to "": absent/unreadable file, no assistant
    entry at all, or an assistant entry whose content is neither a string nor
    a list of text blocks. Never raises.
    """
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > 512_000:
                fh.seek(size - 512_000)
                fh.readline()  # discard the partial line the seek landed inside
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
    except OSError:
        return ""

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
        # This assistant entry carried no usable text (tool_use-only turn) —
        # keep walking backward for an earlier one that did.
        continue
    return ""


#: Heading this op splices into the finishing agent's own report sidecar.
#: Presence of this exact literal is also this op's own idempotency guard
#: (see `_persist_final_report_sync`) — do not reword without updating both.
_PERSISTED_REPORT_HEADING = "## Persisted final report (SubagentStop)\n\n"


def _persist_final_report_sync(worktree_root: str, agent_id: str, text: str) -> None:
    """Best-effort: append `text` to this agent's already-provisioned report
    sidecar, so a lost final-report message is still visible where the
    parent reads before it would otherwise default.

    Reuses `subagent_sandbox.provision_report`'s existing pointer index
    (`_read_sidecar_pointer`, kind="report") to find the sidecar this SAME
    agent_id was provisioned at spawn time — the established
    `state/subagent-share/<session_id>/` convention, not a new location.
    No sidecar was provisioned (ineligible agent type, or provisioning
    itself failed) is the common, silent no-op case: nothing to append to.

    Every failure mode is a silent no-op, never a raise: absent text, no
    provisioned sidecar, an unreadable/unwritable sidecar file, or an
    import failure reaching the pointer index. Idempotent per sidecar via
    `_PERSISTED_REPORT_HEADING`'s presence check, so a duplicate SubagentStop
    delivery for the same agent never double-appends.
    """
    if not text or not agent_id:
        return
    try:
        from coordinator_core.subagent_sandbox.provision_report import _read_sidecar_pointer
    except Exception:  # noqa: BLE001 -- Stop path must never brick on an import
        return
    try:
        rel = _read_sidecar_pointer(worktree_root, agent_id, kind="report")
    except Exception:  # noqa: BLE001 -- pointer resolution reaches foreign code
        rel = None
    if not rel:
        return
    sidecar_path = Path(worktree_root) / rel
    try:
        existing = sidecar_path.read_text(encoding="utf-8")
    except OSError:
        return
    if _PERSISTED_REPORT_HEADING in existing:
        return
    try:
        with open(sidecar_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n" + _PERSISTED_REPORT_HEADING + text.strip() + "\n")
    except OSError:
        return


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


def _append_record_sync(
    store_path: str, entry: dict, session_id: str, sessions_base: str, root: str
) -> None:
    """Append one compact-JSON record line to the per-session store (blocking I/O).

    Called exclusively via asyncio.to_thread() — must not be awaited directly.
    Failures are swallowed: observability loss is preferable to crashing the engine
    on a non-fatal bookkeeping write (mirrors agent_completion_log._append_audit_entry).

    The store directory IS the session directory, so this writer is a session-
    directory CONSTRUCTOR whether it means to be or not. It reaches the hub
    through ``session/core.py::init`` — the directory's one constructor — rather
    than through a bare ``makedirs``, because a directory minted here without a
    ``meta.json`` carries no ``stable_pid`` (K-006's F0 hazard: Layer-1 liveness
    disarmed), is invisible to ``liveness``'s registry-independent arms, and
    silently no-ops every later ``update_meta_field`` write (see that function's
    own docstring for the contract; this cited ``ensure_meta``, deleted
    2026-08-26, until the repoint). ``init`` self-creates the directory and is
    idempotent, so no ``makedirs`` is needed alongside it; both of its resolutions
    are handed over pre-resolved by the caller, so it spawns nothing.

    Negative-spec:
        - Do NOT restore a ``makedirs`` fallback for a failed ``init``. A
          half-initialised session directory is the defect; losing one
          observability line is not.
        - Guard on ``meta.json`` ABSENCE, never on directory absence — another
          bookkeeping writer reaching the hub first is how a record-less
          directory becomes reachable in the first place.
    """
    store_dir = os.path.dirname(store_path)
    if not os.path.isfile(os.path.join(store_dir, "meta.json")):
        try:
            from coordinator_core.session.core import init as _session_init

            _session_init(session_id, sessions_base=sessions_base, root=root or None)
        except Exception as exc:  # noqa: BLE001 -- bookkeeping write, never fatal
            print(
                f"subagent_zero_tool_use: cannot initialise session dir {store_dir}: {exc}",
                file=sys.stderr,
            )
            return
        if not os.path.isfile(os.path.join(store_dir, "meta.json")):
            print(
                f"subagent_zero_tool_use: session dir {store_dir} left uninitialised — "
                "record dropped",
                file=sys.stderr,
            )
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

    Second, independent side-effect: persists the finishing agent's last
    assistant-message text into its own already-provisioned report sidecar
    (see `_persist_final_report_sync`) — closes the gap where a dispatched
    agent's final report is lost in transit and the parent silently defaults
    instead of seeing a visible gap. This leg is a pure best-effort append
    and never influences this op's return value or the tool_use-count write.

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
    # Pure path logic off the hub this handler already resolved — no spawn.
    try:
        _worktree_root = str(main_worktree_root(_sessions_base.parent))
    except Exception:  # noqa: BLE001 -- init resolves it itself on this arm
        _worktree_root = ""

    # Independent leg (AC: robustness) — persists the finishing agent's last
    # assistant text into its own report sidecar, if one was provisioned, so a
    # lost final-report message is not a silent gap. Never affects the
    # tool_use-count durable write above or below: failure here is swallowed
    # entirely inside _persist_final_report_sync.
    if _worktree_root:
        _final_text = await asyncio.to_thread(_last_assistant_text, transcript_path)
        if _final_text:
            await asyncio.to_thread(
                _persist_final_report_sync, _worktree_root, agent_id, _final_text
            )

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

    await asyncio.to_thread(
        _append_record_sync,
        store_path,
        entry,
        session_id,
        str(_sessions_base),
        _worktree_root,
    )

    return no_advisory()
