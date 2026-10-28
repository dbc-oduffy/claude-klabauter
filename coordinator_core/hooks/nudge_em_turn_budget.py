"""
coordinator_core.hooks.nudge_em_turn_budget — advisory op: EM turns before the first engine-ask
fire, and after a green landing.

Purpose: ``advisory_text(params)`` is the leg every dispatcher calls: "" means no signal; a
non-empty string is the advisory AND has already been recorded as delivered, so a caller that does
not emit it must not call it. Each advisory fires once (pre-fire once per session, post-landing
once per landed run). Main thread only; never denies; any failure is silent. Once-only is
best-effort: state is an unlocked read-modify-write, so concurrent hooks may both deliver.
"""

from __future__ import annotations

import asyncio
import os

from coordinator_core._hook_envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op
from coordinator_core.warp_fire import fire_step

GENERATES: list = []

_KILL_SWITCH = "COORDINATOR_HOOK_NUDGE_EM_TURN_BUDGET_DISABLED"


def pre_fire_text(turns: int, bar: int) -> str:
    return (
        f"[em-turn-budget] {turns} turns before the first engine-ask, bar {bar}. " + fire_step()
    )


def post_landing_text(turns: int, bar: int) -> str:
    return (
        f"[em-turn-budget] {turns} turns since the run landed, bar {bar}. "
        "Send a gap through workflow.revise, or fire a follow-up engine-ask whose em_context "
        "quotes the receipt's gap line."
    )


def _advisory(params: dict) -> str:
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root
    from coordinator_core.session import em_turn_count as etc
    from coordinator_core.session import engine_route_state as ers

    payload = payload_of(params)
    if field(payload, "agent_id"):
        return ""
    env = payload.get("env")
    env = env if isinstance(env, dict) else {}
    if (env.get(_KILL_SWITCH) or os.environ.get(_KILL_SWITCH)) == "1":
        return ""
    sid = field(payload, "session_id")
    transcript = field(payload, "transcript_path")
    if not sid or not transcript:
        return ""
    root = _repo_root(payload)
    if root is None:
        return ""
    path = etc.state_path(root, sid)
    state = etc.advance(transcript, etc.load_state(path, transcript))
    turns = state["turns"]
    text = ""
    fired = state.get("fire_turn") is not None
    if fired or (turns > etc.PRE_FIRE_TURNS_MAX and not state.get("pre_fire_signalled")):
        phase = ers.session_route_phase(root, sid, state.get("last_engine_run_id"))
        if phase == ers.PHASE_UNFIRED and not fired:
            if turns > etc.PRE_FIRE_TURNS_MAX and not state.get("pre_fire_signalled"):
                state["pre_fire_signalled"] = True
                from coordinator_core.hooks._em_write_budget import arm_advised

                arm_advised(root, sid)
                text = pre_fire_text(turns, etc.PRE_FIRE_TURNS_MAX)
        elif phase == ers.PHASE_IN_FLIGHT and phase.run_id:
            state["last_engine_run_id"] = phase.run_id
        elif phase == ers.PHASE_LANDED_GREEN and phase.run_id:
            landings = dict(state.get("landings") or {})
            rec = dict(landings.get(phase.run_id) or {})
            if "base_turns" not in rec:
                rec["base_turns"] = turns
            since = turns - rec["base_turns"]
            if since > etc.POST_LANDING_TURNS_MAX and not rec.get("signalled"):
                rec["signalled"] = True
                text = post_landing_text(since, etc.POST_LANDING_TURNS_MAX)
            landings[phase.run_id] = rec
            state["landings"] = landings
    etc.save_state(path, state)
    return text


def advisory_text(params: dict) -> str:
    try:
        return _advisory(params)
    except Exception:
        return ""


@register_op("hooks.nudge_em_turn_budget")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    text = await asyncio.to_thread(advisory_text, params)
    if not text:
        return no_advisory()
    event = field(payload, "hook_event_name") or "PostToolUse"
    return context_only(event, text)
