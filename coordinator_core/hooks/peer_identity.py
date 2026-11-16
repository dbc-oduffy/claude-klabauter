"""SessionStart peer-identity leg: tells a session its roster name and box, and who shares the box.

Name: the live ListAgents roster (`reachability.resolve_candidates(harness_registry.snapshot())`,
the source the SendMessage rewrite hook uses), exact `session_id` match. Box: the session entry's
`unimatrixBox` stamp, else the peer contract's `local_box()` (hostname box id). Never a session-id
prefix and never a name suffix. Advisory: any failure yields no line.

Entrypoints: identity_text(payload) -> Optional[str]; roster_name(session_id) -> Optional[str].
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

_NAME_UNRECORDED = "name unrecorded in roster"
_BOX_UNRECORDED = "box unrecorded"


def _sibling(filename: str, modname: str) -> Any:
    path = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sessions_dir() -> str:
    return os.path.expanduser("~/.claude/sessions")


def _session_entries() -> List[Dict[str, Any]]:
    """Session entries from the sessions dir (one listdir plus one small JSON read each)."""
    sess, out = _sessions_dir(), []
    try:
        files = sorted(os.listdir(sess))
    except OSError:
        return out
    for fname in files:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(sess, fname), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("name"), str) and data.get("sessionId"):
            out.append(data)
    return out


def _roster_candidates() -> list:
    from coordinator_core.session import harness_registry, reachability

    return reachability.resolve_candidates(harness_registry.snapshot())


def roster_name(session_id: str) -> Optional[str]:
    """The roster name of the live session whose id is exactly `session_id`, else None."""
    if not session_id:
        return None
    try:
        for cand in _roster_candidates():
            if cand.session_id == session_id and cand.name:
                return cand.name
    except Exception:
        pass
    for entry in _session_entries():
        if entry.get("sessionId") == session_id:
            return entry["name"]
    return None


def _live_entries() -> List[Dict[str, Any]]:
    """One entry per live roster session: roster candidates joined to their sessions-dir stamps."""
    stamped = {str(e.get("sessionId")): e for e in _session_entries()}
    try:
        cands = _roster_candidates()
    except Exception:
        cands = []
    if cands:
        return [stamped.get(c.session_id) or {"name": c.name, "sessionId": c.session_id} for c in cands]
    return list(stamped.values())


def _fmt(rows: list) -> str:
    return ", ".join(f"{r['name']} ({r['box']})" for r in rows) if rows else "none"


def identity_text(payload: Dict[str, Any]) -> Optional[str]:
    """`You are <name> on <box>` plus the same-box / other-box peer split, else None on any failure."""
    try:
        contract = _sibling("unimatrix-peer-contract.py", "unimatrix_peer_contract_identity")
        box = contract.local_box() or ""
        sid = payload.get("session_id")
        sid = sid if isinstance(sid, str) else ""
        name = roster_name(sid)
        entries = _live_entries()
        me = contract.self_row(entries, sid, box)
        if me is not None:
            box = me["box"] or box
        rows = [contract.agent_row(e, box) for e in entries]
        same, other = contract.split_by_box(rows, box, self_name=name)
        box_text = box or _BOX_UNRECORDED
        if name:
            head = f"PEER IDENTITY: You are {name} on {box_text}."
        else:
            head = f"PEER IDENTITY: {_NAME_UNRECORDED}; box {box_text}."
        return (
            f"{head} Outbound SendMessage text is stamped with this name and box automatically.\n"
            f"Peers on this box: {_fmt(same)}\n"
            f"Peers on other boxes: {_fmt(other)}"
        )
    except Exception:
        return None
