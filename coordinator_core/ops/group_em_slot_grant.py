"""
coordinator_core.ops.group_em_slot_grant -- JSON-RPC "groupem.slot_grant" and "groupem.slot_check".

Purpose: the box Group EM issues or revokes a memory-slot grant for a run's over-threshold step
("groupem.slot_grant"); a run reads whether its grant exists ("groupem.slot_check"). Both wrap
`group_em.slot_grant`.

Registered in `coordinator_core/ops/__init__.py`'s registration list, `_registry_map.py`'s
`OP_MODULE_MAP`, `op_scopes.py` (scope "none") and `authz/classification.py` (slot_grant MUTATING,
slot_check COMPUTE_ONLY). Both enrolled in `_BUDGETED_ENTRYPOINTS` of the spawn audit with an
empty reachable spawn set.

Negative-spec:
    - Never raises: an invalid param or I/O failure is `{"error": ...}`.
    - Spawns nothing; slot_check reads one path and never waits (the wait is client-side).
    - Issue is refused unless the calling session is the live verified box holder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.group_em import slot_grant
from coordinator_core.ipc import register_op


@register_op("groupem.slot_grant")
def _groupem_slot_grant(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.slot_grant" handler.

    Params:
        action (str, optional) -- "issue" (default) or "revoke".
        grantee (str) -- grantee session id, or a registry name resolving to one session.
        run (str) -- run label the grant names.
        declared_peak_mb (number) -- required for issue.
        note (str) -- required for issue.
        ttl_minutes (int, optional) -- issue only; defaults to the module default.

    Returns:
        {"ok": True, "reason", "record"} or {"ok": False, "reason"} on refusal,
        or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    action = p.get("action", "issue")
    grantee, run = p.get("grantee"), p.get("run")
    if action not in ("issue", "revoke"):
        return {"error": "action must be 'issue' or 'revoke'"}
    if not isinstance(grantee, str) or not grantee or not isinstance(run, str) or not run:
        return {"error": "grantee and run (non-empty str) are required"}
    try:
        if action == "revoke":
            res = slot_grant.revoke_slot_grant(grantee, run)
        else:
            kwargs = {}
            if "ttl_minutes" in p:
                kwargs["ttl_minutes"] = p["ttl_minutes"]
            res = slot_grant.issue_slot_grant(
                grantee, run, p.get("declared_peak_mb"), p.get("note", ""), **kwargs
            )
        return {"ok": res.ok, "reason": res.reason, "record": res.record}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.slot_check")
def _groupem_slot_check(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.slot_check" handler.

    Params:
        session_id (str) -- the run's session id.
        run (str) -- run label.

    Returns:
        {"granted": bool, "record": dict | None} or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    sid, run = p.get("session_id"), p.get("run")
    if not isinstance(sid, str) or not sid or not isinstance(run, str) or not run:
        return {"error": "session_id and run (non-empty str) are required"}
    granted, record = slot_grant.check_slot_grant(sid, run)
    return {"granted": granted, "record": record}
