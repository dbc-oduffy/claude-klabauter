"""Audience resolver for guard messages: EM, subagent, or unresolved-as-subagent.

Reuses the discriminator block_reviewer_bash_outside_allowlist.check() runs; no
new identity logic. The EM path (no agent_id) reads no file and imports nothing.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from coordinator_core.guard_messages.alternative_record import Audience


def resolve_audience(payload: Dict[str, Any]) -> Tuple[Audience, Optional[str]]:
    """Return (audience, caller_class).

    No agent_id is EM with caller_class None. A subagent whose identity does not
    resolve is SUBAGENT with caller_class None, never EM: callers must read the
    Audience, not infer EM from a None caller_class.
    """
    if not payload.get("agent_id"):
        return Audience.EM, None

    from coordinator_core.bash_guards._helpers import resolve_git_root
    from coordinator_core.write_guards._subagent_identity import (
        _read_backpointer_subagent_type,
        _resolve_subagent_identity,
    )

    session_id = payload.get("session_id") or ""
    agent_type = payload.get("agent_type") or ""
    try:
        agent_id = _resolve_subagent_identity(str(payload["agent_id"]), session_id)
        if not agent_id:
            return Audience.SUBAGENT, None
        git_root = resolve_git_root(payload.get("cwd")) or resolve_git_root(None)
        subagent_type = ""
        if git_root:
            subagent_type = _read_backpointer_subagent_type(
                git_root, agent_id, expected_em_session_id=session_id
            )
    except Exception:
        return Audience.SUBAGENT, None
    return Audience.SUBAGENT, (subagent_type or agent_type or None)
