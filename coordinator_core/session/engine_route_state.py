"""
coordinator_core.session.engine_route_state — the session's engine-route predicate.

Purpose: answers "has this session sized or launched an engine run" without importing any
hook module's mode logic, so a hook guarding writes and a hook guarding Agent dispatch
share one predicate.
"""

from __future__ import annotations

import os


def session_sizing_state(payload: dict, session_id: str) -> tuple[bool, bool]:
    """(has_sizing, plan_pending): this session touched a sizing object, and one
    of them routed to a plan this session has not touched. Failure is
    (True, False): every caller reads it as "sized", so a probe fault admits."""
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
        return True, False
