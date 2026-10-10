"""
coordinator_core.ops.group_em_box_hold -- JSON-RPC "groupem.box_hold".

Purpose: the Group EM sets or clears the box-wide hold on heavy commands. The admission guard
reads the record directly, so the op carries no read action. Writes
`<settings-home>/state/group-em/box/hold.json` via `group_em.box_hold`.

Registered in `coordinator_core/ops/__init__.py`'s registration list,
`_registry_map.py`'s `OP_MODULE_MAP`, `op_scopes.py` (scope "none") and
`authz/classification.py` (MUTATING). Enrolled in `_BUDGETED_ENTRYPOINTS` of the spawn audit
with an empty reachable spawn set.

Caller identity is NOT enforced: any session may set a hold under any session_id, and any caller
omitting session_id clears any hold. The TTL cap (`MAX_HOLD_TTL_S`) is the only bound.

Negative-spec:
    - Never raises: an invalid param is `{"error": ...}`.
    - Never reads the nomination record or the session registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.group_em import box_hold
from coordinator_core.ipc import register_op


@register_op("groupem.box_hold")
def _groupem_box_hold(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_hold" handler.

    Params:
        action (str) -- "set" or "clear".
        session_id (str) -- required for set; for clear, omitting it is the operator path.
        reason (str, optional) -- set only.
        ttl_s (number, optional) -- set only; clamped to (0, MAX_HOLD_TTL_S].

    Returns:
        set:   {"action": "set", "hold": {...}}
        clear: {"action": "clear", "cleared": bool, "operator": bool}
        or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    action = p.get("action")
    sid = p.get("session_id")
    if action == "set":
        if not isinstance(sid, str) or not sid:
            return {"error": "session_id (non-empty str) is required for action=set"}
        reason = p.get("reason", "")
        if not isinstance(reason, str):
            return {"error": "reason must be a str"}
        ttl = p.get("ttl_s", box_hold.MAX_HOLD_TTL_S)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            return {"error": "ttl_s must be a number"}
        try:
            hold = box_hold.set_hold(sid, reason, ttl)
        except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"action": "set", "hold": {
            "set_by_session": hold.set_by_session,
            "reason": hold.reason,
            "set_at": hold.set_at,
            "expires_at": hold.expires_at,
        }}
    if action == "clear":
        if sid is not None and (not isinstance(sid, str) or not sid):
            return {"error": "session_id must be a non-empty str when given"}
        try:
            cleared = box_hold.clear_hold(sid)
        except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"action": "clear", "cleared": cleared, "operator": sid is None}
    return {"error": "action must be 'set' or 'clear'"}
