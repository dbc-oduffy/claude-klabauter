"""
coordinator_core.session.engine_route_state — the session's engine-route predicate.

Purpose: answers "has this session sized or launched an engine run" without importing any
hook module's mode logic, so a hook guarding writes and a hook guarding Agent dispatch
share one predicate.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PHASE_UNFIRED = "unfired"
PHASE_IN_FLIGHT = "in_flight"
PHASE_LANDED_GREEN = "landed_green"
PHASE_HALTED = "halted"
PHASE_UNKNOWN = "unknown"


class RoutePhase(str):
    """A phase string carrying the engine run id it judged (``run_id``; None when no run was judged)."""

    run_id: Optional[str]

    def __new__(cls, phase: str, run_id: Optional[str] = None) -> "RoutePhase":
        obj = super().__new__(cls, phase)
        obj.run_id = run_id
        return obj


def session_route_phase(repo_root, session_id: str, engine_run_id: Optional[str] = None) -> RoutePhase:
    """The session's engine-ask phase: ``unfired``, ``in_flight``, ``landed_green``, ``halted``, or
    ``unknown`` (silent: callers treat it as no signal). ``.run_id`` is the engine run judged.

    Join: ``session_workflow_run_ids`` yields harness Workflow run ids, which do not index the engine
    record dir. Liveness comes from ``warp_live.live_run_for_session`` (the engine run id). An ended
    run is judged from ``engine_run_id``, the id the caller remembered while the run was live; with
    none remembered the phase is ``unknown``, never a directory scan. A clean landing writes no
    ``halted.json``, so only a ``halted`` marker makes ``halted``; no marker or any other status is
    ``landed_green``. Zero spawns; any exception returns ``unknown``.
    """
    try:
        from coordinator_core import warp_live
        from coordinator_core.hooks.flag_em_poll_in_flight import session_workflow_run_ids
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED

        if not session_workflow_run_ids(session_id):
            return RoutePhase(PHASE_UNFIRED)
        live = warp_live.live_run_for_session(Path(repo_root), session_id)
        if live is not None:
            return RoutePhase(PHASE_IN_FLIGHT, live)
        if not engine_run_id:
            return RoutePhase(PHASE_UNKNOWN)
        marker = halted_run._read(record_dir(Path(repo_root), engine_run_id))
        if marker is not None and marker.get("status") == STATUS_HALTED:
            return RoutePhase(PHASE_HALTED, engine_run_id)
        return RoutePhase(PHASE_LANDED_GREEN, engine_run_id)
    except Exception:
        return RoutePhase(PHASE_UNKNOWN)


class ProbeFault(tuple):
    """The ``(True, False)`` an excepting probe returns. Equal to that tuple, so advisory
    callers read "sized"; a guard tests ``isinstance`` to fail closed instead."""


def session_sizing_state(payload: dict, session_id: str) -> tuple[bool, bool]:
    """(has_sizing, plan_pending): this session touched a sizing object, and one
    of them routed to a plan this session has not touched. An exception returns a
    ``ProbeFault`` ``(True, False)``: advisory callers read it as "sized"; guards must
    check ``isinstance(state, ProbeFault)``."""
    try:
        from coordinator_core.hooks import nudge_unrouted_sizing as sizing

        cwd = payload.get("cwd") or os.getcwd()
        repo_root = sizing._repo_root({"cwd": cwd}) if isinstance(cwd, str) else None
        if not repo_root:
            return True, False
        rels = sizing._session_touched_sizing_files(session_id, repo_root)
        if not rels:
            # A commit releases touched.txt; a Workflow run this session launched
            # outlives it, so a session that fired the route stays sized.
            from coordinator_core.hooks.flag_em_poll_in_flight import session_workflow_run_ids

            return bool(session_workflow_run_ids(session_id)), False
        routes = {(sizing._load_sizing_object(repo_root, rel) or {}).get("route") for rel in rels}
        plan_pending = bool(routes & set(sizing._SKILL_BY_ROUTE)) and not (
            sizing._session_touched_plan_files(session_id, repo_root)
        )
        return True, plan_pending
    except Exception:
        return ProbeFault((True, False))
