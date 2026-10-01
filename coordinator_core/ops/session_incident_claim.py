"""
coordinator_core.ops.session_incident_claim — JSON-RPC "session.incident_claim"
and "session.incident_peers".

Purpose: expose `coordinator_core.session.incident_claims` through the op
registry — claim/release an incident key, and list live peers on it.

Self-registration: importing this module registers both ops. Registered in
`coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES`,
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`,
`coordinator_core/op_scopes.py` (scope "none" — `repo_root` is a wire param,
same story as `session.peer_roster`), and
`coordinator_core/authz/classification.py` (incident_claim MUTATING,
incident_peers COMPUTE_ONLY).

Negative-spec:
    - Discovery only: never SendMessage, never schedule, never assign work.
    - Never commits; the claim is an untracked file under the git common dir.
    - The veneer adds no second try/except around the session module; the
      only translation is the caller-facing re-raise of a key/sid refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.session import incident_claims


def _peer_dict(h: "incident_claims.IncidentHolder", *, with_key: bool) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "session_id": h.session_id,
        "note": h.note,
        "claimed_at": h.claimed_at,
        "address": h.address,
    }
    if with_key:
        row = {"key": h.key, **row}
    return row


@register_op("session.incident_claim")
def _session_incident_claim(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.incident_claim" handler.

    Params: repo_root (str), key (str), note (str, optional),
    release (bool, optional).

    Returns {"key", "own_session_id", "peers": [{session_id, note,
    claimed_at, address}], "released"?: bool}. A bad key or unresolvable
    session id is a JSON-RPC INVALID_PARAMS error.
    """
    p = params if isinstance(params, dict) else {}
    root = p.get("repo_root")
    key = p.get("key")
    if not isinstance(root, str) or not root:
        raise CallerFacingValidationError("repo_root param is required and must be a non-empty string")
    if not isinstance(key, str):
        raise CallerFacingValidationError("key param is required and must be a string")
    note = p.get("note")
    if note is not None and not isinstance(note, str):
        raise CallerFacingValidationError("note param must be a string")

    try:
        if p.get("release"):
            norm = incident_claims.normalize_key(key)
            released = incident_claims.release_claim(root, norm)
            own = incident_claims.core.resolve_session_id(root) or ""
            return {"key": norm, "own_session_id": own, "peers": [], "released": released}
        result = incident_claims.set_claim(root, key, note)
    except ValueError as exc:
        raise CallerFacingValidationError(str(exc)) from exc
    return {
        "key": result.key,
        "own_session_id": result.own_session_id,
        "peers": [_peer_dict(h, with_key=False) for h in result.peers],
    }


@register_op("session.incident_peers")
def _session_incident_peers(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.incident_peers" handler.

    Params: repo_root (str), key (str, optional; absent lists every key).

    Returns {"repo_root", "peers": [{key, session_id, note, claimed_at,
    address}], "error": str|None}. Bad params degrade to peers=[] plus error.
    """
    p = params if isinstance(params, dict) else {}
    root = p.get("repo_root")
    key = p.get("key")
    if not isinstance(root, str) or not root:
        return {"repo_root": "", "peers": [],
                "error": "repo_root param is required and must be a non-empty string"}
    if key is not None and not isinstance(key, str):
        return {"repo_root": root, "peers": [], "error": "key param must be a string"}
    try:
        holders: List[incident_claims.IncidentHolder] = incident_claims.list_peers(root, key)
    except ValueError as exc:
        return {"repo_root": root, "peers": [], "error": str(exc)}
    return {
        "repo_root": root,
        "peers": [_peer_dict(h, with_key=True) for h in holders],
        "error": None,
    }
