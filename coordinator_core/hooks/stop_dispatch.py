"""
coordinator_core.hooks.stop_dispatch — Stop-event fan-in, eight legs (not six).

Purpose: warm-engine composition for DoE-claude's `stop-dispatch.py`, the
single `Stop` hooks.json registration that currently pays one interpreter
start (measured 53.1ms of process time, see the plan's own § Problem) to run
its fan-in of folded scripts every Stop, fleet-wide. This module is the
engine-side op that registration can eventually point at (per this plan's
own exit criterion — the `type`/`url` edit itself is DoE's, not ours).

THE COUNT IS EIGHT, NOT SIX. `stop-dispatch.py`'s own `REGISTRY` (read at
DoE-claude HEAD `3331187b9cd5b806942e6dba290e5985c7dbfc4c`, unchanged at
current HEAD `7b9b78f4b211023e34a9d53f2feaacd45ed98154` — same 478 lines)
carries eight `StopGuard` entries, not the six the classification table's
prose names — the "six" there is inherited docstring prose this plan's own
Anti-scope forbids re-deriving from. Disposition per leg:

  1. `runtime_tripwire_em_check` — CONSUMED, not re-derived. C6 already
     built `hooks.runtime_tripwire_em_check` (`coordinator_core/hooks/
     runtime_tripwire_em_check.py`) for the PostToolUse(Agent) registration.
     Its handler body is NOT event-gated for the three legs it ported
     (subagent-detect, push-failure check, hooks.json-staleness check) — see
     that module's own "COVERAGE ESTABLISHED FIRST" docstring section — so
     the exact same handler is correct for the Stop leg too, matching
     `stop-dispatch.py`'s own `_pre_em_check` reusing the identical script.
     Called here via `_handler({"payload": payload})`, mirroring the
     in-process composition pattern that module's own docstring already
     uses for `git.push_failure_verdict`.

  2. `watchdog_undischarged_next_move` — CONSUMED, not re-derived. C4's
     `hooks.watchdog_undischarged_next_move` already dispatches on payload
     shape: a truthy `tool_name` routes to the PostToolUse(Skill|Agent) leg,
     a payload carrying `transcript_path` (a Stop payload's own shape, no
     `tool_name`) routes to `_handle_stop` — the Stop leg is already built.
     Called here via the same `_handler({"payload": payload})` shape.

  3. `guard_manufactured_blocker` — NOT PORTED. BLOCKING-class per
     `DR-warm-hook-miss-policy` ("Blocking hooks refuse on a miss. Settled
     prior behavior, unchanged by this decision"). `coordinator_core/warm/
     hook_http.py::BLOCKING_EVENTS` is `frozenset({"PreToolUse"})` only —
     `Stop` is NOT a member, so an `http`-flipped Stop registration gets NO
     `unreachable_response` fail-closed treatment on a miss; it fails OPEN
     silently against a dead engine, which is exactly the semantic a
     blocking guard must never have. Widening `BLOCKING_EVENTS` past
     PreToolUse is out of this plan's scope (§ Out of scope) — the answer
     this leg needed ("can it survive today's transport?") is "no", so it
     stays a `command` hook and no engine op is built for it here. A
     `hooks.guard_manufactured_blocker` op existing would not change this
     registration's transport safety, so building one is deferred rather
     than performed as unused residue.

  4. `guard_kira_verdict_routed` — PORTED (residue; no existing op or
     library module covered this script before this chunk). Full verbatim
     port of `guard-kira-verdict-routed.py`'s frontmatter-only decision
     logic below (`_guard_kira_verdict_routed` + helpers). Registered as its
     own op, `hooks.guard_kira_verdict_routed`, for independent testability,
     and composed into the fan-in below.

  5. `stop_em_report_altitude` — COMPOSED via a thin `@register_op` wrapper.
     `coordinator_core/hooks/em_report_altitude.py` already carries the
     full detector logic behind a bare `op(payload) -> dict | None`
     function; its own docstring states plainly it carries no `@register_op`
     handler because Stop events are not routed through the IPC daemon path
     for its ORIGINAL (DoE stdin/stderr shim) transport. That module is
     outside this chunk's `writes:` scope, so the registration wrapper lives
     HERE instead of inside it — `_stop_em_report_altitude_handler` below
     imports and calls `em_report_altitude.op` unchanged.

  6. `nudge_harness_directive_dispatch` — same shape as (5): library `op()`
     exists, no handler, module out of this chunk's `writes:` scope. Wrapped
     here as `hooks.nudge_harness_directive_dispatch`.

  7. `nudge_unrouted_sizing` — same shape as (5)/(6). Wrapped here as
     `hooks.nudge_unrouted_sizing`.

  8. `receiver_state_sensor` — CONSUMED. Already a registered op
     (`hooks.receiver_state_sensor`, `coordinator_core/hooks/
     receiver_state_sensor.py`). The source script's own comment names it a
     PRODUCER: always exits 0 with empty stdout, so it cannot change this
     fan-in's aggregate verdict — composed for its write side-effect only,
     its return value is not folded into the aggregate.

AGGREGATION CONTRACT: mirrors `stop-dispatch.py`'s own CONCATENATE-ALL
(never first-fires-wins) — every composable leg above (all but #3, #8) runs
regardless of whether an earlier leg already produced a block/advisory; one
leg raising is isolated to that leg alone (fail-open for it specifically,
matching the source script's own per-guard `try/except BaseException`).
Return-shape normalisation (`_extract_advisory`) reads every leg's own
`hooks._envelope`-shaped or flat `{"message": str}`-shaped return uniformly:
a `deny()` shape (`permissionDecision: "deny"`) is a BLOCK; anything else
carrying text is an ADVISORY; `no_advisory()`/`None`/no text is silent. If
any leg blocks, the aggregate is `deny("Stop", <joined block reasons>)`;
otherwise, if any leg has advisory text, the aggregate is
`post_advisory(<joined advisory text>)`; otherwise `no_advisory()`. Transport
mapping (this aggregate dict back onto stderr+exit2 / stdout+exit0 for a
DoE-side Stop hook) is explicitly not this chunk's job — see the plan's own
exit criterion ("a one-line type/url edit in a repo we do not own").

Every input comes from `params["payload"]` — never `os.environ` or this
process's own `cwd`/session, matching every other payload-cwd-resolving
`hooks.*` op in this family.

Spec: docs/plans/2026-08-31-six-hook-scripts-become-engine-ops.md, chunk C3
Dispatch brief: state/dispatch-briefs/2026-08-31-six-hook-scripts-become-engine-ops/C3.md
DoE source: coordinator/hooks/scripts/stop-dispatch.py,
    coordinator/hooks/scripts/guard-kira-verdict-routed.py
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import deny, no_advisory, post_advisory
from coordinator_core.hooks.em_report_altitude import op as _em_report_altitude_op
from coordinator_core.hooks.guard_kira_verdict_routed import (
    _guard_kira_verdict_routed,
    _guard_kira_verdict_routed_handler,
)
from coordinator_core.hooks.nudge_harness_directive_dispatch import (
    op as _nudge_harness_directive_dispatch_op,
)
from coordinator_core.hooks.nudge_unrouted_sizing import op as _nudge_unrouted_sizing_op
from coordinator_core.hooks.receiver_state_sensor import _handler as _receiver_state_sensor_handler
from coordinator_core.hooks.runtime_tripwire_em_check import _handler as _runtime_tripwire_em_check_handler
from coordinator_core.hooks.watchdog_undischarged_next_move import _handler as _watchdog_undischarged_next_move_handler
from coordinator_core.ipc import register_op

# ---------------------------------------------------------------------------
# guard-kira-verdict-routed.py — the decision logic previously ported inline
# here (docs/plans/2026-08-31-six-hook-scripts-become-engine-ops.md chunk
# C3) now lives in its own registered module,
# `coordinator_core.hooks.guard_kira_verdict_routed` (W4-C14,
# docs/plans/2026-09-18-doe-holds-no-scripts.md), so `hook-run` can dial
# `hooks.guard_kira_verdict_routed` directly. `_guard_kira_verdict_routed_handler`
# is imported from there and composed into this fan-in unchanged.
# `_guard_kira_verdict_routed` itself is re-imported (not just its handler)
# so this module's own name keeps resolving for existing callers/tests that
# reach it as `stop_dispatch._guard_kira_verdict_routed`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Thin @register_op wrappers for the three library modules whose own `op()`
# carries no handler (out of this chunk's `writes:` scope — see module
# docstring items 5-7).
# ---------------------------------------------------------------------------


def _wrap_flat_op(op_fn) -> dict:
    """Call a flat `op(payload) -> dict | None` function and normalise its
    return to a hooks._envelope-shaped dict for uniform aggregation below.
    These three library ops are NON-BLOCKING BY CONSTRUCTION (see each
    module's own docstring) — their `{"message": str}` return is always an
    advisory, never a block."""
    def _handler(params: dict, repo_root=None) -> dict:
        try:
            payload = params.get("payload")
            if not isinstance(payload, Mapping):
                payload = {}
            result = op_fn(dict(payload))
        except Exception:
            return no_advisory()
        if not isinstance(result, dict):
            return no_advisory()
        message = result.get("message")
        if not isinstance(message, str) or not message:
            return no_advisory()
        return post_advisory(message)
    return _handler


# Review: overengineering-reviewer (Kira) — these three keys had no
# registration, dispatch site, or cross-module caller (grepped across
# claude-klabauter and DoE-claude); DoE's own hook shims import
# `coordinator_core.hooks.<module>.op` directly and never go through
# `_REGISTRY`. @register_op removed from all three; the plain functions
# the fan-in below actually calls are unchanged.
_stop_em_report_altitude_handler = _wrap_flat_op(_em_report_altitude_op)
_nudge_harness_directive_dispatch_handler = _wrap_flat_op(
    _nudge_harness_directive_dispatch_op
)
_nudge_unrouted_sizing_handler = _wrap_flat_op(_nudge_unrouted_sizing_op)


# ---------------------------------------------------------------------------
# Aggregation: normalise every leg's own return shape uniformly, then
# CONCATENATE-ALL per the source dispatcher's own contract (see module
# docstring "AGGREGATION CONTRACT").
# ---------------------------------------------------------------------------


def _extract_advisory(result) -> "tuple[bool, Optional[str]]":
    """Return (is_block, text) from any hooks.* envelope-shaped or flat
    {"message": str}-shaped return used by this fan-in's composed legs.

    A `deny()` shape (`permissionDecision: "deny"`) is a BLOCK; any other
    shape carrying non-empty text (`additionalContext`, or a flat
    `message`) is an ADVISORY; `no_advisory()` / `None` / no text is silent.
    """
    if not isinstance(result, dict):
        return False, None
    hso = result.get("hookSpecificOutput")
    if isinstance(hso, dict):
        if hso.get("permissionDecision") == "deny":
            reason = hso.get("permissionDecisionReason")
            return True, reason if isinstance(reason, str) and reason else None
        text = hso.get("additionalContext")
        return False, text if isinstance(text, str) and text else None
    message = result.get("message")
    if isinstance(message, str) and message:
        return False, message
    return False, None


@register_op("hooks.stop_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """Stop fan-in: compose the seven composable legs (all but the excluded
    BLOCKING-class `guard_manufactured_blocker`) and aggregate CONCATENATE-
    ALL, per module docstring.

    `repo_root` (the framework-supplied handler argument) is unused — every
    composed leg resolves its own repo root from `params["payload"]["cwd"]`,
    matching every other payload-cwd-resolving `hooks.*` op in this family.
    One leg raising is isolated to that leg alone (fail-open for it
    specifically); this handler itself never raises.
    """
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    payload = dict(payload)
    leg_params = {"payload": payload}

    block_reasons: list = []
    advisories: list = []

    for leg_call in (
        lambda: _runtime_tripwire_em_check_handler(leg_params),
        lambda: _watchdog_undischarged_next_move_handler(leg_params),
        lambda: _guard_kira_verdict_routed_handler(leg_params),
        lambda: _stop_em_report_altitude_handler(leg_params),
        lambda: _nudge_harness_directive_dispatch_handler(leg_params),
        lambda: _nudge_unrouted_sizing_handler(leg_params),
    ):
        try:
            result = leg_call()
        except Exception:
            continue
        is_block, text = _extract_advisory(result)
        if is_block:
            # Review: coordinator:code-reviewer — a block must survive the
            # fold even with a falsy reason; decoupling is_block from text
            # would let a deny("Stop", "") evaporate silently.
            block_reasons.append(text or "<no reason given>")
        elif text:
            advisories.append(text)

    # receiver_state_sensor — PRODUCER only (see module docstring item 8):
    # composed for its write side-effect; its return is never folded into
    # this aggregate's verdict. It is "common_dir"-scoped, so under normal
    # IPC dispatch its `repo_root` handler arg is `git_common_dir(request_
    # repo)` (ipc.py::resolve_op_repo_key) — resolved here explicitly from
    # the SAME payload["cwd"] every other leg above already reads, since
    # this in-process call bypasses that resolution. Never the ambient
    # process cwd: leaving `repo_root` at its own default here previously
    # wrote a `sess-1` entry into THIS repo's own `.git/coordinator-sessions/`
    # from a `tmp_path`-rooted test payload (caught by
    # coordinator_core/conftest.py's live-session-hub litter guard).
    try:
        cwd = payload.get("cwd")
        common_dir = None
        if isinstance(cwd, str) and cwd:
            git_root = show_toplevel(cwd)
            if git_root:
                common_dir = resolve_git_common_dir(git_root)
        sensor_params = {
            "session_id": payload.get("session_id") or "",
            "transcript_path": payload.get("transcript_path") or "",
            "delegation_evidence": "false",
        }
        await _receiver_state_sensor_handler(sensor_params, repo_root=common_dir)
    except Exception:
        pass

    if block_reasons:
        return deny("Stop", "\n\n".join(block_reasons))
    if advisories:
        return post_advisory("\n\n".join(advisories))
    return no_advisory()
