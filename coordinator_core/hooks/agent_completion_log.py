"""
coordinator_core.hooks.agent_completion_log — PostToolUse write hook op.

Purpose: record every Agent-tool dispatch in the coordinator audit trail.
Fires on PostToolUse for the Agent tool — one compact JSON line appended to
.git/coordinator-sessions/logs/agent-audit.jsonl.

A ROW IS A DISPATCH-OUT RECORD, NOT AN ARRIVAL — the op name says "completion"
for cross-repo reasons (it is the JSON-RPC method id "hooks.agent_completion_log",
wired from DoE's hooks.json shim; renaming it breaks that seam for no behavioural
gain), but PostToolUse fires when the *Agent tool call* returns, and for a
backgrounded or named dispatch that is the moment the dispatch goes OUT, not the
moment the subagent finishes. `logged_at` is therefore dispatch time; a row for an
agent that was later killed with TaskStop, or that is still running, is present by
construction. Consumers: this file carries NO arrival information whatsoever and
cannot support a "did it complete?" check — DoE built exactly that cross-check
(their runtime-tripwire skip-if-completed, wiki § 2026-06-09) on the old wording of
this docstring; it was vacuous by construction and produced 189 false
`em-side-trigger-loss` fires in this repo's own state/runtime-tripwire-fire-log.tsv.
Source: cross-repo/inbox/2026-07-30-doe-claude-em-trigger-loss-nudge-reply.md.

The -c compact (one-line-JSON) format is REQUIRED: downstream consumers grep
for `"agentId":"<id>"` on a single line. Pretty-printed output would break
that consumer.

Negative-spec:
    This op carries NO /tmp fallback — the resident engine is always repo-keyed;
    the no-git-root branch from the source shell hook is unreachable here.
    # engine always repo-keyed; /tmp fallback unreachable — no-op rather than
    # write outside .git/

    Do NOT return advisory text — this is a write op (side-effect is the
    disk append); always return no_advisory().

    Do NOT read tool_response as a nested object — inputs are flat scalars
    extracted by the mcp_tool manifest shim (see R-1 comment below).

R-1: dispatched_agent_id is the flattened tool_response.agentId, pending
claude-central-em shim confirmation; op is dormant-correct if unflattenable.

Spec backlink: pln-pcore-08-async-bookkeeping-hoo-7920d5 § C3
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

from coordinator_core._hook_envelope import payload_of
from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir


GENERATES: list = []


def _append_audit_entry(log_file: str, entry: dict) -> None:
    log_dir = os.path.dirname(log_file)
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        print(f"agent_completion_log: cannot create log dir {log_dir}: {exc}", file=sys.stderr)
        return
    try:
        # REQUIRED: downstream consumers grep for `"agentId":"<id>"` on a
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with open(log_file, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"agent_completion_log: cannot write {log_file}: {exc}", file=sys.stderr)
        return


@register_op("hooks.agent_completion_log")
async def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)

    import asyncio

    if not repo_root:
        return no_advisory()

    try:
        _sessions_base = git_common_dir(repo_root) / "coordinator-sessions"
    except RuntimeError:
        _sessions_base = Path(str(repo_root)) / "coordinator-sessions"
    log_file = str(_sessions_base / "logs" / "agent-audit.jsonl")

    description = field(params, "description") or "unknown"
    subagent_type = field(params, "subagent_type") or "general-purpose"
    name_val = field(params, "name") or None
    dispatched_agent_id = field(params, "dispatched_agent_id") or field(params, "dispatched_agent_id_snake") or None

    logged_at = (
        datetime.datetime.now(tz=datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    entry = {
        "logged_at": logged_at,
        "description": description,
        "subagent_type": subagent_type,
        "name": name_val,
        "agentId": dispatched_agent_id,
    }

    await asyncio.to_thread(_append_audit_entry, log_file, entry)

    return no_advisory()

run = _handler
