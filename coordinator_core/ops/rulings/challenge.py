"""JSON-RPC "deferral.challenge": test deferrals and named reasons against the closed list and flag the EM.

Params: surface (reviewer_unapplied | agent_report | halt_record | plan), text, fields (object), session_id (optional).
Reply: {verdicts: [{span, kind, reason_class, verdict, why}], flag_em, flag_ref?}; `flag_ref` is {path, line} of the queue row.

Negative-spec:
  - Advisory: never denies, no deny_registry entry; content never raises.
  - No spawn, no git: pure classification plus one O_APPEND write when flag_em.
  - A missing session_id queues under `_unattributed`, never an error.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.rulings.challenge import challenge
from coordinator_core.rulings.em_flags import append_flag

_SURFACES = ["reviewer_unapplied", "agent_report", "halt_record", "plan"]

_SCHEMA = {
    "type": "object",
    "properties": {
        "surface": {"type": "string", "enum": _SURFACES, "description": "Where the text came from."},
        "text": {"type": "string", "description": "Reviewer, agent-report or halt-record text to challenge."},
        "fields": {"type": "object", "description": "Structured deferral fields: unapplied_reason, deferrals[]."},
        "session_id": {"type": "string", "description": "EM flag queue key; absent maps to _unattributed."},
    },
    "required": ["surface", "text"],
    "additionalProperties": False,
}


@register_op("deferral.challenge", params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deferral.challenge". See module docstring."""
    if repo_root is None:
        raise CallerFacingValidationError("deferral.challenge: no repo root")
    surface = params.get("surface")
    text = params.get("text")
    fields = params.get("fields")
    session_id = params.get("session_id")
    if surface not in _SURFACES:
        raise CallerFacingValidationError(f"deferral.challenge: surface must be one of {_SURFACES}")
    if not isinstance(text, str):
        raise CallerFacingValidationError("deferral.challenge: text must be a string")
    if fields is not None and not isinstance(fields, dict):
        raise CallerFacingValidationError("deferral.challenge: fields must be an object")
    if session_id is not None and not isinstance(session_id, str):
        raise CallerFacingValidationError("deferral.challenge: session_id must be a string")

    reply = challenge(surface, text, fields)
    if reply["flag_em"]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "surface": surface,
            "session_id": session_id,
            "challenges": [v for v in reply["verdicts"] if v["verdict"] == "challenge"],
        }
        reply["flag_ref"] = append_flag(Path(main_worktree_root(repo_root)), session_id, record)
    return reply
