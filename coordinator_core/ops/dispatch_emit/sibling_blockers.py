"""
coordinator_core.ops.dispatch_emit.sibling_blockers -- which fan-out sibling run holds an open spine row.

Purpose: a close whose plan keeps a spine row open because a sibling run has not landed what the
row waits on names that sibling and the call that refires this run. File reads only.

Negative-spec:
  - No git spawn, no subprocess, no write; a ``running`` sibling reads landed through
    ``run_close.release_view`` without reconcile.
  - Never parses an executor's free-text blocker.
  - No parent record or no status file for the run: ``[]``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def _posix(path: Any) -> str:
    return str(path).replace("\\", "/").removeprefix("./") if isinstance(path, str) else ""


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sizing_plan(root: Path, slice_sizing: str) -> Optional[str]:
    import yaml

    try:
        data = yaml.safe_load((root / slice_sizing).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    plan = data.get("plan") if isinstance(data, dict) else None
    return _posix(plan) or None


def sibling_blockers(root: Path, run_id: str, plan_rel: str) -> list[dict]:
    """One entry per open spine row of *plan_rel* that waits on a non-landed fan-out sibling.

    Entry keys: ``row``, ``run_id``, ``label``, ``status``, ``plan`` (sibling plan or None),
    ``via`` (``depends_on_plan`` or ``writes``), ``refire`` (``warp_fire.fire_command`` of this
    run's slice sizing). ``[]`` when the run has no status file or parent record, or no open row
    resolves to a sibling. A ``depends_on_plan`` edge onto a sibling's plan wins over a declared-writes
    overlap; one entry per (row, sibling).
    """
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    base = Path(root)
    own = _read_json(base / contract.status_rel(run_id))
    parent_id = own.get("parent_id") if own else None
    if not isinstance(parent_id, str) or not parent_id:
        return []
    parent_data = _read_json(base / contract.parent_record_rel(parent_id))
    if parent_data is None:
        return []
    try:
        parent = contract.ParentRecord.from_json(parent_data)
    except (ValueError, KeyError, TypeError):
        return []
    mine = next((r for r in parent.runs if r.run_id == run_id), None)
    others = [r for r in parent.runs if r.run_id != run_id]
    if mine is None or not others:
        return []

    from coordinator_core.archive_stamp import _open_spine_row_ids

    plan_path = base / plan_rel
    open_ids = _open_spine_row_ids(plan_path)
    if not open_ids:
        return []

    from coordinator_core.ops.dispatch_emit import run_close
    from coordinator_core.ops.plan_tasks_render import load_rows

    try:
        rows = load_rows(plan_path.read_text(encoding="utf-8")).rows
    except (OSError, UnicodeDecodeError):
        return []
    by_id = {str(r.get("id")): r for r in rows}

    statuses, _ = run_close.release_view(base, parent, own_run_id=run_id)
    pending = [r for r in others if statuses.get(r.run_id) != contract.STATUS_LANDED]
    if not pending:
        return []

    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.sibling_hold import _declared_product_writes
    from coordinator_core.warp_fire import fire_command

    refire = fire_command(mine.slice_sizing)
    plans = {r.run_id: _sizing_plan(base, r.slice_sizing) if r.slice_sizing else None for r in pending}
    sibling_writes: dict[str, set[str]] = {}
    for r in pending:
        try:
            manifest = _read_json(record_dir(base, r.run_id) / "manifest.json")
        except ValueError:
            manifest = None
        sibling_writes[r.run_id] = set(_declared_product_writes(manifest)) if manifest else set()

    out: list[dict] = []
    for row_id in open_ids:
        row = by_id.get(row_id)
        if row is None:
            continue
        edge_plans = {
            _posix(e.get("plan")) for e in (row.get("depends_on_plan") or ()) if isinstance(e, dict)
        } - {""}
        writes = {_posix(w) for w in (row.get("writes") or ()) if isinstance(w, str)} - {""}
        for r in pending:
            sib_plan = plans[r.run_id]
            if sib_plan and sib_plan in edge_plans:
                via = "depends_on_plan"
            elif writes & sibling_writes[r.run_id]:
                via = "writes"
            else:
                continue
            out.append(
                {
                    "row": row_id,
                    "run_id": r.run_id,
                    "label": r.label,
                    "status": statuses.get(r.run_id, r.status),
                    "plan": sib_plan,
                    "via": via,
                    "refire": refire,
                }
            )
    return out
