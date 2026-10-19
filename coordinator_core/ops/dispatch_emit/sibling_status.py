"""
coordinator_core.ops.dispatch_emit.sibling_status -- ``dispatch.sibling_status``, the read-only
view of a fan-out's sibling statuses and currently released fire calls.

Purpose: an EM whose run's terminal commit landed outside the workflow script never saw
``sibling_release``'s reply; this op recomputes it from the same ``run_close.release_view``.

Params: ``{parent_id}`` or ``{run_id}`` (a sibling's id; its parent is resolved from its status
file or tracked parent copy). Reply: ``{exit_code, applied: false, parent_id, siblings:
[{run_id, label, status}], released: [fire call], blocked, superseded?}``, siblings and released in
fanout order.

Negative-spec:
  - Writes no file: no ``reconcile_running``, no ``claimed_write``; a ``running`` sibling whose
    terminal commit is on HEAD reads ``landed`` without being rewritten.
  - Unknown or unsafe id is a refusal, never a guess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import run_close

SIBLING_STATUS_OP = "dispatch.sibling_status"


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


def _parent_of_run(root: Path, run_id: str) -> Optional[str]:
    status = run_close._read_json(root / contract.status_rel(run_id))
    pid = status.get("parent_id") if status else None
    if isinstance(pid, str) and pid:
        return pid
    own = run_close._own_parent_id(root, run_id, contract)
    if own is not None:
        return own
    for rec in sorted((root / Path(contract.parent_record_rel("x")).parents[1]).glob("*/parent.json")):
        data = run_close._read_json(rec)
        runs = data.get("runs") if data else None
        if isinstance(runs, list) and any(isinstance(r, dict) and r.get("run_id") == run_id for r in runs):
            return rec.parent.name
    return None


def sibling_status(params: dict, root: Path) -> dict:
    """Return the fan-out's sibling statuses and released fire calls for ``parent_id`` or ``run_id``."""
    parent_id = params.get("parent_id")
    run_id = params.get("run_id")
    if bool(parent_id) == bool(run_id):
        return _err("dispatch.sibling_status: pass exactly one of parent_id or run_id")
    for label, value in (("parent_id", parent_id), ("run_id", run_id)):
        if value is not None and (not isinstance(value, str) or not safe_id(value)):
            return _err(f"dispatch.sibling_status: {label} {value!r} is not a single safe path segment")
    base = Path(root)
    if run_id:
        parent_id = _parent_of_run(base, run_id)
        if parent_id is None or not safe_id(parent_id):
            return _err(f"dispatch.sibling_status: no parent record found for run {run_id}")
    data = run_close._read_json(base / contract.parent_record_rel(parent_id))
    if data is None:
        return _err(f"dispatch.sibling_status: no parent record for {parent_id}")
    try:
        parent = contract.ParentRecord.from_json(data)
    except (ValueError, KeyError, TypeError):
        return _err(f"dispatch.sibling_status: unreadable parent record for {parent_id}")
    statuses, view = run_close.release_view(base, parent)
    reply: dict[str, Any] = {
        "exit_code": 0,
        "applied": False,
        "parent_id": parent.parent_id,
        "siblings": [
            {"run_id": r.run_id, "label": r.label, "status": statuses[r.run_id]} for r in parent.runs
        ],
        "released": view["released"],
        "blocked": view["blocked"],
    }
    if "superseded" in view:
        reply["superseded"] = view["superseded"]
    return reply


@register_op(SIBLING_STATUS_OP)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.sibling_status" handler: `sibling_status` over the repo root."""
    if repo_root is None:
        return _err("dispatch.sibling_status: repo_root is required")
    return sibling_status(params, repo_root)
