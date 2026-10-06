"""coordinator_core.hooks.preuse_sendmessage_dispatch — PreToolUse(SendMessage)
advisory for chatty workflow runs.

Advisory only: never a permission decision, no caps, no counters. Fail open to
`no_advisory()` on every internal error.

A sender is in a chatty run when `<run-dir>/roster.json` exists and lists the
stdin `agent_id` as a member. `transcript_path` is the PARENT session's
transcript; the run dir is the parent of the single match of
`<transcript_path minus .jsonl>/subagents/workflows/*/agent-<agent_id>.jsonl`
(0 or >1 matches: no-op). The roster's counters and caps are never touched. The agent's
first send gets one note; the "already advised" fact is an O_EXCL sentinel
`<run-dir>/advised/<agent_id>`, run-scoped and outside live repo state.

Hot path: one JSON read and one file create, no subprocess spawn.

Kill-switch: env COORDINATOR_HOOK_PREUSE_SENDMESSAGE_DISPATCH_DISABLED=1.

Op contract: `params` is the flat PreToolUse payload dict (`tool_name`,
`agent_id`, `transcript_path`, …).

Spec backlink: coordinator-content-repo docs/plans/2026-09-30-opt-in-chatty-workflows-ersatz-agent-teams.md
"""

from __future__ import annotations

import glob
import json
import os
import re

from coordinator_core._hook_envelope import context_only, no_advisory, payload_of
from coordinator_core.ipc import register_op

_KILL_SWITCH = "COORDINATOR_HOOK_PREUSE_SENDMESSAGE_DISPATCH_DISABLED"
_AGENT_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_ROSTER = "roster.json"
_ADVISED_DIR = "advised"

_ADVICE = (
    "Each message costs its reader context. Summarise; don't relay."
)


def _is_member(roster_path: str, agent_id: str) -> bool:
    with open(roster_path, "rb") as fh:
        roster = json.load(fh)
    members = roster.get("members") if isinstance(roster, dict) else None
    if not isinstance(members, list):
        return False
    return any(isinstance(m, dict) and m.get("agent_id") == agent_id for m in members)


def _claim_once(run_dir: str, agent_id: str) -> bool:
    advised = os.path.join(run_dir, _ADVISED_DIR)
    os.makedirs(advised, exist_ok=True)
    try:
        fd = os.open(
            os.path.join(advised, _AGENT_ID_SAFE_RE.sub("_", agent_id)),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        return False
    os.close(fd)
    return True


@register_op("hooks.preuse_sendmessage_dispatch")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        if os.environ.get(_KILL_SWITCH) == "1":
            return no_advisory()
        if params.get("tool_name") != "SendMessage":
            return no_advisory()
        agent_id = params.get("agent_id")
        transcript = params.get("transcript_path")
        if not (isinstance(agent_id, str) and agent_id):
            return no_advisory()
        if not (isinstance(transcript, str) and transcript):
            return no_advisory()
        if not transcript.endswith(".jsonl"):
            return no_advisory()
        safe_id = glob.escape(agent_id)
        matches = glob.glob(
            os.path.join(
                glob.escape(transcript[: -len(".jsonl")]),
                "subagents", "workflows", "*", f"agent-{safe_id}.jsonl",
            )
        )
        if len(matches) != 1:
            return no_advisory()
        run_dir = os.path.dirname(matches[0])
        roster_path = os.path.join(run_dir, _ROSTER)
        if not os.path.isfile(roster_path) or not _is_member(roster_path, agent_id):
            return no_advisory()
        if not _claim_once(run_dir, agent_id):
            return no_advisory()
        return context_only("PreToolUse", _ADVICE)
    except Exception:
        return no_advisory()
