"""
coordinator_core.hooks.guard_em_prefire_survey -- GUARD-EM-PREFIRE-SURVEY.

Purpose: warn on an EM's exploratory reads and searches made to fill em_context before its first
engine-ask fire (advisory, never a deny), and deny its spot-check sprawl after a green landing.
``evaluate(params)`` returns a deny or advisory envelope or None. Fail-open: any failure, a missing
input or a disarm returns None, never a deny. Main thread only; a subagent is never counted.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

from coordinator_core._hook_envelope import context_only, deny, no_advisory, payload_of
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.warp_fire import NAG_REPEAT_PREFIX, fire_step, nag_text

GENERATES: list = []

PREFIRE_EXPLORATORY_CALLS_MAX = 8  # absolute on every rung: XL made 51 pre-fire calls and L 26; doctrine forbids reading to fill em_context, so those counts are survey, not need; 8 cuts XL 84%, L 69%
POSTLANDING_VERIFY_CALLS_MAX = 3
_KILL_SWITCH = "COORDINATOR_HOOK_EM_PREFIRE_SURVEY_DISABLED"
ARMING = "role-marker"  # arms on the em-role-asserted marker; "strict-only" also requires COORDINATOR_STRICT_DISPATCH == "1"; only tests select it
GUARD_NAME = "guard-em-prefire-survey"
DENY_PREFIX = "[em-prefire-survey]"
_ADVISORY_MARK = "\x00advisory\x00"  # _decide's text carries it when the leg warns instead of denying


def prefire_advisory_text(n: int, budget: int, session_id: str = "") -> str:
    """Full text on the session's first nag; a repeat is the one terse counted line. No session id: full."""
    if not session_id:
        return (
            f"{DENY_PREFIX} {n} exploratory calls since the last turn, budget {budget} (all). "
            + fire_step() + " Add em_context."
        )
    nag = nag_text(session_id)
    if nag.startswith(NAG_REPEAT_PREFIX):
        return nag
    return (
        f"{DENY_PREFIX} {n} exploratory calls since the last turn, budget {budget} (all). "
        + nag + " Add em_context."
    )


def postlanding_deny_text(n: int, budget: int) -> str:
    return (
        f"{DENY_PREFIX} {n} exploratory calls since the landing, budget {budget} (verify). "
        "Send the gap: coordinator-invoke workflow.revise --params-file <file> (exempt), "
        "or fire a follow-up engine-ask quoting its gap line."
    )


def _repo_root(cwd: object) -> Optional[str]:
    if not isinstance(cwd, str) or not cwd:
        return None
    probe = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent


def _env_value(payload: dict, key: str) -> str:
    env = payload.get("env")
    env = env if isinstance(env, dict) else {}
    return str(env.get(key) or os.environ.get(key) or "")


def _settings_home(payload: dict) -> Optional[str]:
    forwarded = _env_value(payload, "COORDINATOR_SETTINGS_HOME")
    if forwarded:
        return forwarded
    try:
        from coordinator_core._settings_home import settings_home

        return str(settings_home())
    except Exception:
        return None


def _armed(payload: dict, root: str, sid: str) -> bool:
    """Armed only for a session carrying the em-role-asserted marker (every EM session, DoE EMs
    included). Trap: an absent marker means unarmed, so a session whose SessionStart never wrote it
    is never denied."""
    if ARMING == "strict-only" and _env_value(payload, "COORDINATOR_STRICT_DISPATCH") != "1":
        return False
    from coordinator_core.hooks.support import turn_intent
    from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir

    common = resolve_git_common_dir(root)
    return bool(common) and turn_intent.role_marker_present(turn_intent.session_dir(common, sid))


def _is_group_em_holder(root: str, sid: str) -> bool:
    """True only for the live, verified group-em-nomination holder of this repo. Trap: the holder
    check runs only when the record's session_id equals sid, because entry_status can fall through
    to transcript lookups; any failure means not the holder (fail toward armed)."""
    try:
        from coordinator_core.group_em import nomination

        rec = nomination.read_record(root)
        if not isinstance(rec, dict) or rec.get("session_id") != sid:
            return False
        if nomination.entry_status(rec)["status"] != "verified":
            return False
        return bool(nomination.is_live(rec).live)
    except Exception:
        return False


_GEM_STAMP = re.compile(r"\[G-EM [^\s\]]+ [0-9a-fA-F]{8}\]")


def _gem_stamped(opening: object) -> bool:
    """True when the window-opening turn carries a "[G-EM <name> <sid8>]" directive stamp: a scoped
    G-EM directive started the session, so its reads are the directive's work, not a survey."""
    return isinstance(opening, str) and _GEM_STAMP.search(opening) is not None


def _decide(payload: dict) -> Optional[str]:
    """The deny text, or None. Raises are the caller's fail-open."""
    from coordinator_core.session import em_prefire_window as epw
    from coordinator_core.session import em_turn_count as etc
    from coordinator_core.session import engine_route_state as ers

    if field(payload, "agent_id"):
        return None
    if _env_value(payload, _KILL_SWITCH) == "1":
        return None
    sid = field(payload, "session_id")
    transcript = field(payload, "transcript_path")
    root = _repo_root(payload.get("cwd"))
    if not sid or not transcript or root is None:
        return None
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if tool_name not in epw.COUNTED_TOOLS:
        return None
    if not _armed(payload, root, sid):
        return None
    if _is_group_em_holder(root, sid):
        return None
    path = epw.state_path(root, sid)
    home = _settings_home(payload)
    prior = epw.load_state(path, transcript)
    current_id = field(payload, "tool_use_id") or None
    state = epw.advance(
        transcript, prior, current_id, settings_home=home, current_call=(tool_name, tool_input), repo_root=root
    )
    pre_now, all_now = epw.classify(
        tool_name,
        tool_input,
        state.get("opening") or "",
        home,
        named=state.get("named"),
        written=state.get("written"),
        repo_root=root,
    )
    if state != prior:
        epw.save_state(path, state)
    if _gem_stamped(state.get("opening")):
        return None
    if not (pre_now or all_now):
        return None

    pre_n = len(state["pre"]) if pre_now else 0
    over = pre_n > PREFIRE_EXPLORATORY_CALLS_MAX
    if not over and not state.get("fire_seen"):
        return None

    last_run = (etc.read_state(root, sid, transcript) or {}).get("last_engine_run_id")
    phase = ers.session_route_phase(root, sid, last_run)
    if phase == ers.PHASE_UNFIRED:
        if not over or state.get("fire_seen") or state.get("fired") or state.get("disarmed"):
            return None
        from coordinator_core.hooks._em_write_budget import arm_advised

        arm_advised(root, sid)
        return _ADVISORY_MARK + prefire_advisory_text(pre_n, PREFIRE_EXPLORATORY_CALLS_MAX, sid)
    if phase in (ers.PHASE_LANDED_GREEN, ers.PHASE_HALTED) and phase.run_id:
        landings = dict(state.get("landings") or {})
        rec = landings.get(phase.run_id)
        if not isinstance(rec, dict) or rec.get("window") != state.get("window"):
            rec = {"window": state.get("window"), "fires": state.get("fires"), "touched": []}
            landings[phase.run_id] = rec
            state["landings"] = landings
            epw.save_state(path, state)
        if state.get("revised") or int(state.get("fires") or 0) > int(rec.get("fires") or 0):
            return None
        # Only calls touching the landed run are charged; `touched` holds their tool_use ids.
        touched = rec.get("touched")
        touched = list(touched) if isinstance(touched, list) else []
        extra = 0
        if epw.touches_landed_run(tool_name, tool_input, phase.run_id, root):
            if current_id:
                if current_id not in touched:
                    touched.append(current_id)
                    rec["touched"] = touched
                    state["landings"] = landings
                    epw.save_state(path, state)
            else:
                extra = 1
        since = len(touched) + extra
        if since > POSTLANDING_VERIFY_CALLS_MAX:
            return postlanding_deny_text(since, POSTLANDING_VERIFY_CALLS_MAX)
    return None


def _evaluate_payload(payload: dict) -> Optional[dict]:
    """Deny envelope for an already-normalised event dict, else None. Any failure returns None."""
    try:
        text = _decide(payload)
        if not text:
            return None
        if text.startswith(_ADVISORY_MARK):
            return context_only("PreToolUse", text[len(_ADVISORY_MARK):])
        return apply_guard_level(GUARD_NAME, deny("PreToolUse", text)) or None
    except Exception:
        return None


def evaluate(params: dict) -> Optional[dict]:
    """Deny envelope for an over-budget survey call, else None. Any failure returns None."""
    return _evaluate_payload(payload_of(params))


@register_op("hooks.guard_em_prefire_survey")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    try:
        envelope = await asyncio.to_thread(_evaluate_payload, payload)
    except Exception:
        envelope = None
    return envelope if envelope else no_advisory()
