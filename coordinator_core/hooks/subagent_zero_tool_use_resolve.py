
from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core._hook_envelope import payload_of
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
    params = payload_of(params)
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
