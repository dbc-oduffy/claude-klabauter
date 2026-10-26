"""The one route line every dead end names for a halted run whose work landed.

``route_line`` is pure; ``halted_route_for`` is one marker read; ``halted_route_for_sizing`` reuses
the ``halted_run`` finders. No subprocess, no git.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

ROUTE_OP = "dispatch.record_halted_run_landed"


def route_line(run_id: str, sha: Optional[str] = None) -> str:
    """The sentence naming the close call; a None *sha* reads ``<the halt commit>``."""
    slot = sha if sha else "<the halt commit>"
    return f"If its work landed, record it: {ROUTE_OP} {{run_id: {run_id}, sha: {slot}}}."


def _halted_marker_strict(root: Path, run_id: object) -> Optional[dict]:
    """The run's halt marker when it reads ``halted``, else None; `UnreadableHaltRecord` when corrupt.
    An unsafe run id is no run."""
    if not isinstance(run_id, str):
        return None
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED

    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError:
        return None
    marker = halted_run.read_strict_run(record_dir)
    return marker if marker is not None and marker.get("status") == STATUS_HALTED else None


def _halted_marker(root: Path, run_id: object) -> Optional[dict]:
    """`_halted_marker_strict` reading a corrupt marker as not halted (route lines only)."""
    from coordinator_core.ops.dispatch_emit import halted_run

    try:
        return _halted_marker_strict(root, run_id)
    except halted_run.UnreadableHaltRecord:
        return None


def halted_route_for(root: Path, run_id: str) -> Optional[str]:
    """``route_line(run_id)`` when the run's halt marker reads ``halted``, else None."""
    return route_line(run_id) if _halted_marker(root, run_id) is not None else None


def route_suffix(root: Path, run_id: object) -> str:
    """`` <route line>`` when *run_id* names a halted run, else the empty string."""
    return f" {route_line(run_id)}" if _halted_marker(root, run_id) is not None else ""


def halted_refusal(root: Path, run_id: object, missing: str) -> Optional[str]:
    """The halted-run refusal message (``no <missing> exists to land``) for a halted *run_id*, else None."""
    from coordinator_core.ops.dispatch_emit import halted_run

    try:
        marker = _halted_marker_strict(root, run_id)
    except halted_run.UnreadableHaltRecord as exc:
        return f"{exc.reason}: the halt marker is unreadable; no {missing} exists to land until it is repaired."
    if marker is None:
        return None
    return f"{run_id} halted at {marker.get('halted') or 'a halt'}; no {missing} exists to land. {route_line(run_id)}"


def land_only_commit_route(root: Path, run_id: object) -> Optional[dict]:
    """The `halted_by` payload (`ask_contract.HALTED_RECORD_PARAM`) for committing a halted run's
    withheld finished work, else None (not halted, or nothing withheld). Reads the marker only."""
    marker = _halted_marker(root, run_id)
    if marker is None:
        return None
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD

    kind = marker.get("halted")
    try:
        withheld = warp_run_deliverable.halted_withheld_paths(root, run_id)
    except halted_run.UnreadableHaltRecord:
        return None
    if not isinstance(kind, str) or not kind or not withheld:
        return None
    payload: dict = {"halted": kind}
    plan = marker.get(HALT_PLAN_FIELD)
    if isinstance(plan, str) and plan:
        payload[HALT_PLAN_FIELD] = plan
    return payload


def halted_route_for_sizing(root: Path, sizing_rel: str, sha: Optional[str] = None) -> Optional[str]:
    """``route_line`` for the halted run a refire of *sizing_rel* stands behind, else None."""
    from coordinator_core.ops.dispatch_emit import halted_run

    run_id = halted_run.halted_ask_run_for(root, sizing_rel) or halted_run.halted_run_for(root, sizing_rel)
    return route_line(run_id, sha) if run_id else None
