"""
coordinator_core.hooks.subagent_zero_tool_use_resolve — Stage 3 (pull/poll resolve) op.

Purpose: Make the zero-tool-use verdict for ONE dispatched agent resolvable at a
PostToolUse-Agent moment we control, instead of depending on the `SubagentStop` push
event having reached the caller. `SubagentStop` is an OPEN, upstream-declined harness
bug (GH #25147, #33049 — both closed as not-planned) that silently stops firing on
some builds; when it does not fire, Stage 1 (`hooks.subagent_zero_tool_use`) never
writes a record, and any consumer that only ever waits on that push is left reporting
a blanket UNKNOWN for every agent in the session, whether or not Stage 1 actually ran
for that particular dispatch.

This op does not change how Stage 1 writes or Stage 2 reads. It adds a third read:
given one `agent_id`, poll the SAME per-session store Stage 1 writes and Stage 2
surfaces, at a moment the caller controls (e.g. the existing PostToolUse Agent-tool
hook, which already fires reliably and already carries `agent_id` —
see hooks.track_dispatched_agents / hooks.agent_completion_log). Resolving per-agent
rather than dumping the whole store closes the gap for every agent whose Stage-1
write DID land (SubagentStop is not pinned as the sole trigger; coordinator-claude's own spike found
it fires for most backgrounded dispatches, just not guaranteed to), and gives a loud,
specific, actionable reason for the remainder — never a bare "UNKNOWN".

Naming note: the underlying store (written by hooks.subagent_zero_tool_use) holds one
record per verified tool-use count, not only zero — see that module's docstring,
"Naming note". This op is the one place in the trio that already does the
`tool_use_count == 0` filtering a caller needs; hooks.subagent_zero_tool_use_surface
deliberately does not (it returns the raw per-session record list unfiltered).

Verdicts (pinned):
    "did-work"       — a record for this agent_id exists with tool_use_count > 0.
    "zero-tool-use"  — a record for this agent_id exists with tool_use_count == 0.
    "unknown"        — no resolvable record; `reason` always names the store path
                        checked and the specific cause (missing input, unreadable
                        store, no record for this agent, or a malformed count field).

Negative-spec:
    Does NOT write anything — pure read/compute, same posture as
    hooks.subagent_zero_tool_use_surface. No new store, no new schema; reads the
    identical `<git_common_dir>/coordinator-sessions/<session_id>/
    subagent-zero-tool-use.jsonl` file Stage 1 already writes.

    Does NOT perform "dispatched but no record ever will arrive" reconciliation across
    the whole session — that stays coordinator-claude-side per the cross-repo contract. This op only
    answers "what does the store say about THIS agent_id, right now."

    Does NOT retry, wait, or poll on a timer — a single point-in-time read. If Stage 1
    has not written yet when this is called, the caller gets an honest "unknown, not
    yet recorded" rather than a busy-loop; calling again later (the same PostToolUse
    hook re-firing is not expected, but a manual re-check is cheap and safe) is fine
    because this is a pure read with no state mutation.

    Design-as-offers: `reason` always leads with the next action ("redispatch <id>",
    "verify the deliverable manually", "no action needed") rather than only naming
    the failure.

Spec backlink: cross-repo/inbox/2026-07-25-coordinator-claude-em-zero-tool-use-detection-verdict-viable.md
    (constraint 3 — "do not pin SubagentStop as the sole trigger")
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir

_RECORD_KIND = "zero-tool-use"


def _verdict(kind: str, agent_id: str, reason: str, *, tool_use_count=None, store_path: str = "") -> dict:
    return {
        "verdict": kind,
        "agent_id": agent_id,
        "tool_use_count": tool_use_count,
        "reason": reason,
        "store_path": store_path,
    }


def _resolve_store_sync(store_path: str, agent_id: str) -> dict:
    """Read the per-session store and resolve the verdict for one agent_id (blocking I/O).

    Called exclusively via asyncio.to_thread() — must not be awaited directly.
    Mirrors hooks.subagent_zero_tool_use_surface's parse tolerance (malformed lines
    and foreign-`kind` lines are skipped, never fatal), but resolves to a single
    per-agent verdict instead of returning the whole store.
    """
    if not os.path.exists(store_path):
        return _verdict(
            "unknown",
            agent_id,
            f"no zero-tool-use store at {store_path} — SubagentStop never fired this "
            f"session (trigger-loss); verify {agent_id}'s deliverable manually or "
            "redispatch it to be safe.",
            store_path=store_path,
        )

    try:
        with open(store_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return _verdict(
            "unknown",
            agent_id,
            f"could not read {store_path}: {exc} — verify {agent_id}'s deliverable "
            "manually or redispatch it to be safe.",
            store_path=store_path,
        )

    # Last matching record wins — Stage 1 writes at most one record per verified
    # dispatch, but taking the last (not first) is defensive against an agent_id
    # being reused by a later redispatch within the same session.
    matched: dict | None = None
    other_agent_records = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        if parsed.get("kind") != _RECORD_KIND:
            continue
        if parsed.get("agent_id") == agent_id:
            matched = parsed
        else:
            other_agent_records += 1

    if matched is None:
        detail = (
            f" ({other_agent_records} record(s) for other agents found there)"
            if other_agent_records
            else " (store is present but empty of zero-tool-use records)"
        )
        return _verdict(
            "unknown",
            agent_id,
            f"no zero-tool-use record for agent {agent_id} in {store_path}{detail} — "
            "SubagentStop did not fire for this dispatch (trigger-loss); verify the "
            f"deliverable manually or redispatch {agent_id} to be safe.",
            store_path=store_path,
        )

    count = matched.get("tool_use_count")
    if not isinstance(count, int) or isinstance(count, bool):
        return _verdict(
            "unknown",
            agent_id,
            f"record for agent {agent_id} in {store_path} has a non-integer "
            f"tool_use_count ({count!r}) — treating as unresolved; verify the "
            f"deliverable manually or redispatch {agent_id} to be safe.",
            store_path=store_path,
        )

    if count == 0:
        return _verdict(
            "zero-tool-use",
            agent_id,
            f"agent {agent_id} completed with 0 verified tool calls — it did no "
            f"work; redispatch {agent_id}.",
            tool_use_count=0,
            store_path=store_path,
        )

    return _verdict(
        "did-work",
        agent_id,
        f"agent {agent_id} made {count} verified tool call(s) — real work done, no "
        "action needed.",
        tool_use_count=count,
        store_path=store_path,
    )


@register_op("hooks.subagent_zero_tool_use_resolve")
async def _handler(params: dict, repo_root=None) -> dict:
    """Compute-only op: resolve the zero-tool-use verdict for ONE agent_id, by polling
    the Stage-1 store directly rather than waiting on a SubagentStop-triggered push.

    Inputs (flat scalar, extracted via _payload.field(); "" treated as absent):
        session_id, agent_id, hook_event_name.

    Returns the pinned {"verdict", "agent_id", "tool_use_count", "reason",
    "store_path"} shape directly (structured JSON-RPC result, not an advisory
    envelope) — never raises; every failure path resolves to verdict "unknown" with
    a specific, actionable `reason` naming the path checked and the cause.
    """
    import asyncio

    session_id = field(params, "session_id")
    agent_id = field(params, "agent_id")

    if not agent_id:
        return _verdict("unknown", "", "no agent_id supplied — nothing to resolve.")
    if not session_id:
        return _verdict(
            "unknown",
            agent_id,
            f"no session_id supplied — cannot locate the store to check for {agent_id}.",
        )
    if not repo_root:
        return _verdict(
            "unknown",
            agent_id,
            f"no repo_root supplied — cannot locate the store to check for {agent_id}.",
        )

    try:
        _sessions_base = git_common_dir(repo_root) / "coordinator-sessions"
    except RuntimeError:
        _sessions_base = Path(str(repo_root)) / "coordinator-sessions"
    store_path = str(_sessions_base / session_id / "subagent-zero-tool-use.jsonl")

    return await asyncio.to_thread(_resolve_store_sync, store_path, agent_id)
