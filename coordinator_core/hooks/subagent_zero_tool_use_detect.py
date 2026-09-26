"""
coordinator_core.hooks.subagent_zero_tool_use_detect — SubagentStop
engine op, the gate+relay leg.

Purpose: warm command/native-door counterpart of DoE-claude's
`coordinator/hooks/scripts/subagent-zero-tool-use-detect.py`. That script's
whole job — per its own module docstring's DR-047 transport-seam framing —
was THIN PLUMBING: gate on `agent_type`/own-session membership, select
`agent_transcript_path` (never the decoy `transcript_path`, AC10), and relay
to three engine ops (`hooks.subagent_zero_tool_use`,
`hooks.subagent_review_mark`, `hooks.receiver_state_sensor`), all of which
already exist in this package and are unchanged by this chunk. Per this
plan's own premise (every hook lands command/native-door behind `hook-run`,
W4-C1 verdict), that gate+relay logic itself becomes an engine op here
rather than staying a DoE-resident shim — composing the three existing legs
in-process, matching the fan-in shape `stop_dispatch`/
`postuse_stop_family_dispatch` already use for a sibling event.

AC10, carried over unchanged: `agent_transcript_path` is authoritative;
`transcript_path` on a SubagentStop payload is the PARENT session's
transcript (valid, tool-call-rich, and therefore a false-negative generator
if read even as a fallback) — never read here, not even as an `or` fallback.

Op contract: `params["payload"]` is the SubagentStop payload dict
(`session_id`, `agent_id`, `agent_type`, `agent_transcript_path`,
`hook_event_name`, `cwd`, ...) — never `os.environ` or this process's own
`cwd`. Always returns `no_advisory()` (empty dict): a SubagentStop emission
is invisible to the EM (DEC-4 in the source script), so this op never
surfaces advisory text and never blocks — the durable-record writes the
three composed ops perform are its entire product.

Ordered gates (order is load-bearing, matching the source script):
  1. `agent_type` absent/empty -> silent, before any other work.
  2. Own-session filter -- the arriving `agent_id` must be a member of THIS
     session's own `dispatched-agents.txt` (peer-session subagents also fire
     this event and must be excluded). Reads fail CLOSED (not a member).

Graceful degradation: any failure resolving the repo root, the common dir,
or composing a leg degrades to `no_advisory()` — a broken sensor invocation
must never surface as a tool-call failure, matching every composed leg's
own fail-soft contract.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C14
DoE source: coordinator/hooks/scripts/subagent-zero-tool-use-detect.py
"""

from __future__ import annotations

import os
import re
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks.receiver_state_sensor import _handler as _receiver_state_sensor_handler
from coordinator_core.hooks.subagent_review_mark import _handler as _subagent_review_mark_handler
from coordinator_core.hooks.subagent_zero_tool_use import _handler as _subagent_zero_tool_use_handler
from coordinator_core.ipc import register_op

# paths -- mirrors the source script's own `_ID_CHARSET_RE`.
_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9_@-]+$")


def _is_dispatched_this_session(git_root: "Optional[str]", session_id: str, agent_id: str) -> bool:
    if not git_root or not session_id or not agent_id:
        return False
    if not _ID_CHARSET_RE.match(session_id):
        return False

    common_dir = resolve_git_common_dir(git_root)
    if not common_dir:
        return False

    dispatch_file = os.path.join(
        common_dir, "coordinator-sessions", session_id, "dispatched-agents.txt"
    )
    try:
        if not os.path.isfile(dispatch_file):
            return False
        with open(dispatch_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                if cols and cols[0] == agent_id:
                    return True
    except Exception:
        return False
    return False


@register_op("hooks.subagent_zero_tool_use_detect")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)

    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        return no_advisory()

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = ""
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str):
        agent_id = ""

    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = ""

    try:
        git_root = show_toplevel(cwd) if cwd else show_toplevel(os.getcwd())
    except Exception:
        git_root = None

    if not _is_dispatched_this_session(git_root, session_id, agent_id):
        return no_advisory()

    agent_transcript_path = payload.get("agent_transcript_path")
    if not isinstance(agent_transcript_path, str):
        agent_transcript_path = ""

    common_dir = None
    try:
        if git_root:
            common_dir = resolve_git_common_dir(git_root)
    except Exception:
        common_dir = None

    leg_params = {
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "agent_transcript_path": agent_transcript_path,
    }

    try:
        await _subagent_zero_tool_use_handler(leg_params, repo_root=common_dir)
    except Exception:
        pass

    try:
        await _subagent_review_mark_handler(
            dict(leg_params, cwd=cwd), repo_root=common_dir
        )
    except Exception:
        pass

    try:
        await _receiver_state_sensor_handler(
            {
                "session_id": session_id,
                "transcript_path": agent_transcript_path,
                "delegation_evidence": "false",
            },
            repo_root=common_dir,
        )
    except Exception:
        pass

    return no_advisory()
