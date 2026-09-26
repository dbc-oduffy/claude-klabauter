
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.claim_state import resolve_claim_state

_BLITZ_RE = re.compile(r"blitz|mise-en-place|bug-blitz", re.IGNORECASE)

_BASE_SYSTEM = {
    "provenance_completeness": "complete",
    "capture_source": "derived_handoff_lineage",
    "completeness": "complete",
}


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _claimed_by(
    path: str,
    fm: Dict[str, Any],
    *,
    repo_root: Optional[Path] = None,
) -> Optional[Any]:
    mirror_fallback = fm.get("claimed_by")
    if mirror_fallback is None:
        mirror_fallback = fm.get("consumed_by")

    abs_path = (Path(repo_root) / path) if repo_root is not None else Path(path)
    try:
        state = resolve_claim_state(abs_path, repo_root=repo_root)
    except Exception:
        return mirror_fallback

    return state.holder if state.holder is not None else mirror_fallback


def _jq_truthy(value: Any) -> bool:
    return value is not None and value is not False


def _system_block(created_by_session: str) -> Dict[str, Any]:
    """Build the ``system`` sub-object, conditionally merging ``created_by_session``.

    Mirrors the jq idiom of re-emitting the WHOLE ``system`` dict (not patching
    one key) when ``$session`` (``CREATED_BY_SESSION``) is non-empty — a plain
    dict literal in Python produces the identical JSON shape either way, so this
    is a non-issue here, but the re-merge shape is preserved for auditability
    against the jq source.
    """
    if len(created_by_session) > 0:
        return {**_BASE_SYSTEM, "created_by_session": created_by_session}
    return dict(_BASE_SYSTEM)


def derive(
    handoffs_active: List[Dict[str, Any]],
    handoffs_archived: List[Dict[str, Any]],
    created_by_session: str = "",
    *,
    repo_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Derive the session-hierarchy record list from queried handoff records.

    Args:
        handoffs_active: ``[{"path": str, "frontmatter": dict}, ...]`` from
            ``--type handoff``.
        handoffs_archived: same shape, from ``--type handoff-archived``.
        created_by_session: ``CS_SESSION_ID`` env value, or ``""`` if unset.
            Non-empty triggers the conditional ``system.created_by_session``
            merge on every emitted record (jq: ``if ($session | length) > 0``).
        repo_root: optional, forwarded to ``_claimed_by``'s ledger-first
            accessor to resolve each record's repo-relative ``path`` to an
            absolute one. The current caller
            (``coordinator_core.ops.session_hierarchy_derive``) does not pass
            this yet; omitted, the accessor falls back to its own
            cwd-relative resolution, with the in-memory frontmatter mirror as
            the final fallback (see ``_claimed_by`` docstring).

    shell-doc-ok: quotes the jq oracle's own filter expression, where `$session`
    is a jq variable rather than a shell expansion.

    Returns:
        session records first, then synthetic workstream-type nodes appended
        (emission order is part of the contract — see module negative-spec).
    """
    all_handoffs = list(handoffs_active) + list(handoffs_archived)

    pred_lookup: Dict[str, Optional[str]] = {}
    for rec in all_handoffs:
        fm = rec.get("frontmatter") or {}
        pred_lookup[_basename(rec["path"])] = _claimed_by(rec["path"], fm, repo_root=repo_root)

    consumed = [
        rec
        for rec in all_handoffs
        if _claimed_by(rec["path"], rec.get("frontmatter") or {}, repo_root=repo_root) is not None
    ]

    session_records: List[Dict[str, Any]] = []
    for rec in consumed:
        path = rec["path"]
        fm = rec.get("frontmatter") or {}

        workstream_raw = fm.get("workstream") or ""
        session_type = "blitz" if _BLITZ_RE.search(workstream_raw) else "session"

        pred_raw = fm.get("predecessor")
        pred = pred_raw if _jq_truthy(pred_raw) else "none"
        parent_session_id: Optional[str]
        if pred in ("none", None, ""):
            parent_session_id = None
        else:
            parent_session_id = pred_lookup.get(pred) or None

        record: Dict[str, Any] = {
            "session_id": _claimed_by(path, fm, repo_root=repo_root),
            "session_type": session_type,
            "workstream": fm.get("workstream") or "unknown",
            "parent_session_id": parent_session_id,
            "linked_handoffs": [path],
        }
        if _jq_truthy(fm.get("branch")):
            record["branch"] = fm["branch"]
        record["system"] = _system_block(created_by_session)
        session_records.append(record)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in session_records:
        groups.setdefault(rec["session_id"], []).append(rec)

    merged_records: List[Dict[str, Any]] = []
    seen_sids: set[str] = set()
    for rec in session_records:
        sid = rec["session_id"]
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        group = groups[sid]

        winner = max(group, key=lambda r: _basename(r["linked_handoffs"][0]))

        parent_session_id = winner["parent_session_id"]
        if parent_session_id is None:
            for candidate in group:
                if candidate["parent_session_id"] is not None:
                    parent_session_id = candidate["parent_session_id"]
                    break

        linked_handoffs = sorted({r["linked_handoffs"][0] for r in group})

        merged: Dict[str, Any] = {
            "session_id": sid,
            "session_type": winner["session_type"],
            "workstream": winner["workstream"],
            "parent_session_id": parent_session_id,
            "linked_handoffs": linked_handoffs,
        }
        if "branch" in winner:
            merged["branch"] = winner["branch"]
        merged["system"] = winner["system"]
        merged_records.append(merged)

    session_records = merged_records

    workstreams = sorted({(rec.get("frontmatter") or {}).get("workstream") or "unknown" for rec in consumed})

    workstream_nodes: List[Dict[str, Any]] = []
    for ws in workstreams:
        node: Dict[str, Any] = {
            "session_id": "workstream:" + ws,
            "session_type": "workstream",
            "workstream": ws,
            "parent_session_id": None,
            "linked_handoffs": [],
            "system": _system_block(created_by_session),
        }
        workstream_nodes.append(node)

    return session_records + workstream_nodes
