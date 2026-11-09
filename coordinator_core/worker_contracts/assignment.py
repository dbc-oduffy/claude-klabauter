"""Pure structural assignment validation, shared before wrapper and model spawn.

This does not grant capacity or establish authority. The supervisor still checks
the actual branch, quiet-box snapshot, live Group EM grant and capacity ledger.
"""
from __future__ import annotations


def validate_assignment(assignment: object, *, worker: bool = False) -> None:
    from coordinator_core.group_em.admission_post import valid_peak_mb

    if not isinstance(assignment, dict):
        raise ValueError("assignment must be an object")
    if any(not isinstance(assignment.get(key), str) or not assignment[key].strip()
           for key in ("session_id", "run")) or not valid_peak_mb(assignment.get("declared_peak_mb")):
        raise ValueError("assignment requires session_id, run, and positive declared_peak_mb")
    if not worker:
        return
    if assignment.get("source") != "pm":
        raise ValueError("assignment requires PM source")
    if not isinstance(assignment.get("branch"), str) or not assignment["branch"].strip() or assignment["branch"] in {"main", "master"}:
        raise ValueError("PM assignment requires a non-main branch")
    if assignment.get("quiet_box") is not True:
        raise ValueError("assignment requires quiet_box")
    if not isinstance(assignment.get("evidence"), str) or not assignment["evidence"].strip():
        raise ValueError("assignment requires PM evidence")
    limit = assignment.get("quiet_max_sessions")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("assignment requires positive quiet_max_sessions")
