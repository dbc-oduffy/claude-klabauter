"""coordinator_core.hooks.unsized_writer_dispatch_deny -- the main loop's Agent dispatch of a
product-writing agent (the executor family, general-purpose) in a session with no sizing and no
engine run is refused, as is one after the session's XS lane took a projected deny and no
Workflow run exists.

Purpose: leaf of ``hooks.block_unenumerated_agent_type.check``. Hand edits are budgeted by the
code-dispatch guard; a hand-dispatched writer would sidestep that budget unless gated here.
Invariant: no module-scope import beyond stdlib and ``hooks._envelope``
(``test_fleet_mode_reader_set.py`` counts every import edge into the guard closure); never import
``hooks.nudge_em_code_dispatch``. ``GUARD_NAME`` is the code-dispatch guard's own name, so both
share one level.

Order is load-bearing: tool, agent_id and the type are checked before any session read, so no
other Agent dispatch pays anything. Zero subprocess spawns.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from coordinator_core.hooks._envelope import deny

GUARD_NAME = "nudge-em-code-dispatch"
WRITER_TYPES = frozenset({"coordinator:executor", "executor", "general-purpose"})


def is_writer_type(subagent_type: str) -> bool:
    """True for the executor family (any ``*-executor`` after the namespace) and general-purpose."""
    return subagent_type in WRITER_TYPES or subagent_type.rsplit(":", 1)[-1].endswith("-executor")


_AFTER_BUDGET_REASON = "XS budget denied a hand edit this session; writers run inside the engine. "


def _xs_budget_denied(payload: Mapping[str, Any], sid: str) -> bool:
    """True when the session has no Workflow run and its xs lane record took >= 1 projected deny.
    Any exception reads False: a probe failure never becomes a deny."""
    try:
        from coordinator_core.hooks import flag_em_poll_in_flight
        from coordinator_core.hooks import nudge_unrouted_sizing
        from coordinator_core.session import xs_lane_budget

        if flag_em_poll_in_flight.session_workflow_run_ids(sid):
            return False
        root = nudge_unrouted_sizing._repo_root({"cwd": payload.get("cwd")})
        if root is None:
            return False
        rec = xs_lane_budget.read_lane(root, sid)
        return bool(
            rec and rec.get("kind") == xs_lane_budget.KIND_XS and xs_lane_budget.deny_count(rec) >= 1
        )
    except Exception:
        return False


def unsized_writer_dispatch_deny(
    payload: Mapping[str, Any], subagent_type: str
) -> Optional[dict]:
    """None unless a top-level writer-type Agent dispatch runs in a session that is unsized.

    Sized means a sizing touched or a Workflow run launched this session
    (``engine_route_state.session_sizing_state``, called through the module so tests can patch
    it). A sized session with no Workflow run and an xs lane record with ``denies >= 1`` also
    denies. A ``ProbeFault`` result (probe raised) also denies, naming the failure. The deny
    goes through ``apply_guard_level`` so the guard level governs it.
    """
    if (
        payload.get("tool_name") != "Agent"
        or payload.get("agent_id")
        or not is_writer_type(subagent_type)
    ):
        return None
    from coordinator_core.hooks.dispatch_escape import _resolve_session_id_op
    from coordinator_core.session import engine_route_state
    from coordinator_core.warp_fire import fire_step

    sid = _resolve_session_id_op(dict(payload))
    state = engine_route_state.session_sizing_state(dict(payload), sid)
    probe_failed = isinstance(state, engine_route_state.ProbeFault)
    if state[0] is True and not probe_failed:
        if _xs_budget_denied(payload, sid):
            from coordinator_core.machine_profile import apply_guard_level

            return apply_guard_level(GUARD_NAME, deny("PreToolUse", _AFTER_BUDGET_REASON + fire_step()))
        return None
    from coordinator_core.machine_profile import apply_guard_level

    if probe_failed:
        reason = "Route-state probe failed; sizing unknown. Route via engine-ask. " + fire_step()
    else:
        reason = (
            "No sizing or engine run this session; product-writing agents run inside it. "
            + fire_step()
        )
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", reason))
