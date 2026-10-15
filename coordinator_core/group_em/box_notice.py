"""coordinator_core.group_em.box_notice -- the box-wide Group EM notice text and its recipients.

The standing is box-wide: every live named session is told once when a holder takes the role and
once when it ends. This module owns the one message text and the two recipient readers; the CLI
`coordinator/bin/group-em-box-notice.py` and the workstream-complete stand-down op call it.

NEGATIVE SPEC: never sends, never spawns. `recipients_from_registry` reads the harness session
registry; the CLI's `claude agents --json` enumeration is the slower, equivalent source and stays
the CLI default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.group_em import session_registry

NOTICE_ENTERED = "entered"
NOTICE_ENDED = "ended"


def box_recipients(
    agents: list[dict[str, Any]], session_id: str, skip_session_ids: frozenset = frozenset()
) -> list[dict[str, str]]:
    """Every live NAMED session in `agents` except the holder and `skip_session_ids`.

    A name is the only address `SendMessage` takes; an unnamed background session has none and is
    not addressable, so it is not listed. Deduplicated by session id."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        sid, name = agent.get("sessionId"), agent.get("name")
        if not isinstance(sid, str) or not sid or sid == session_id or sid in skip_session_ids:
            continue
        if not isinstance(name, str) or not name or sid in seen:
            continue
        seen.add(sid)
        out.append({"name": name, "sessionId": sid, "cwd": str(agent.get("cwd") or "")})
    return out


def recipients_from_registry(
    session_id: str,
    skip_session_ids: frozenset = frozenset(),
    directory: Optional[Path] = None,
) -> list[dict[str, str]]:
    """`box_recipients` over the session registry: rows whose pid is running and that carry a
    name. Spawn-free."""
    agents = [
        {"sessionId": row.session_id, "name": row.name, "cwd": row.cwd}
        for row in session_registry.read_rows(directory)
        if row.name and session_registry.pid_alive(row.pid)
    ]
    return box_recipients(agents, session_id, skip_session_ids)


def render_notice(kind: str, holder: dict[str, Optional[str]]) -> str:
    """The one message text. Asks nothing; says no reply is wanted."""
    who = f"{holder['name']} (session {holder['sessionId']})" if holder.get("name") else (
        f"session {holder['sessionId']}"
    )
    if kind == NOTICE_ENTERED:
        reach = (
            f"Reach it with SendMessage to '{holder['name']}'."
            if holder.get("name")
            else "It has no session name yet; address it by session id."
        )
        return (
            f"Group EM notice, informational, no reply is wanted: {who} now holds the box-wide "
            f"Group EM standing. {reach} Route parked-wave reports, blockers, cross-session "
            "obligations, box-capacity and queueing questions, and a second opinion before asking "
            "the PM there; settled asks stay on the memo channel. This replaces any earlier "
            "Group EM address you were given."
        )
    return (
        f"Group EM notice, informational, no reply is wanted: {who} has stood down and the box "
        "has no Group EM now. Do not route to that address. The next /group-em entry announces "
        "its own."
    )
