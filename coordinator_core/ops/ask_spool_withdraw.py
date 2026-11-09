"""coordinator_core.ops.ask_spool_withdraw -- op "ask_spool.withdraw": retire one pending
spooled engine-ask by id and record why.

A thin wrapper over ``ask_spool.record.withdraw``; the entry is renamed, never deleted, so the
reason stays beside the payload. Only a pending entry is retired; a claimed or minted one is
reported by state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op

OP_KEY = "ask_spool.withdraw"

SPOOL_ID_PATTERN = r"^[0-9a-f]{16}$"

_SCHEMA = {
    "type": "object",
    "properties": {
        "spool_id": {"type": "string", "pattern": SPOOL_ID_PATTERN,
                     "description": "The id named in the 'spooled as <id>' refusal text."},
        "reason": {"type": "string", "minLength": 1,
                   "description": "Why the entry is withdrawn; recorded in its header."},
        "repo_root": {"type": "string",
                      "description": "Repo whose spool holds the entry; default the caller's worktree."},
    },
    "required": ["spool_id", "reason"],
    "additionalProperties": False,
}


@register_op(OP_KEY, params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ask_spool.withdraw": params `spool_id`, `reason`, `repo_root` (optional).

    Returns {"spool_id", "state", "withdrawn"}; state is "withdrawn", or the entry's state
    ("claimed", "minted", "absent") when it could not be retired.
    """
    import re

    from coordinator_core.ask_spool import record

    spool_id = params.get("spool_id")
    reason = params.get("reason")
    if not isinstance(spool_id, str) or not re.fullmatch(SPOOL_ID_PATTERN, spool_id):
        raise CallerFacingValidationError("ask_spool.withdraw: spool_id must be 16 lowercase hex characters")
    if not isinstance(reason, str) or not reason.strip():
        raise CallerFacingValidationError("ask_spool.withdraw: reason must be a non-empty string")
    root = params.get("repo_root") or repo_root
    if not root:
        raise CallerFacingValidationError("ask_spool.withdraw: no repo root")
    return record.withdraw(str(root), spool_id, reason)
