"""
coordinator_core.hooks.subagent_arrival_record -- SubagentStop write op.

Appends one arrival line per stopped subagent through
``coordinator_core.arrival_record.append_arrival``. Unconditional: it never
reads the log, and it is a sibling of, never a replacement for,
``hooks.subagent_arrival_check`` (the transcript-tail pull op).

Negative-spec:
    Fail open. A missing session_id or agent_id, an unsafe id, or any error
    writes nothing and returns ``no_advisory()``.
    Do NOT return advisory text: the shim batch relays this op's response and
    must stay silent.
"""

from __future__ import annotations

import asyncio
import datetime

from coordinator_core._hook_envelope import payload_of
from coordinator_core.arrival_record import append_arrival
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir


GENERATES: list = []


def _record(repo_root, session_id: str, agent_id: str, agent_type: str) -> None:
    stopped_at = datetime.datetime.now(tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    append_arrival(
        git_common_dir(repo_root), session_id, agent_id, agent_type, stopped_at
    )


@register_op("hooks.subagent_arrival_record")
async def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        session_id = field(params, "session_id")
        agent_id = field(params, "agent_id")
        if not repo_root or not session_id or not agent_id:
            return no_advisory()
        agent_type = field(params, "agent_type")
        await asyncio.to_thread(_record, repo_root, session_id, agent_id, agent_type)
    except Exception:
        pass
    return no_advisory()


run = _handler
