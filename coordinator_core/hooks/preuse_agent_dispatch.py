"""coordinator_core.hooks.preuse_agent_dispatch — PreToolUse(Agent) fan-in
op: two registered guards, one call.

Arrival note (W4-C8, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/preuse-agent-dispatch.py`, a
subprocess fan-in over four registered `hooks.json` scripts (each folded via
`importlib.util.spec_from_file_location` + captured-stdout/stderr replay).
None of that subprocess-avoidance plumbing applies here: every guard below
is already a same-repo, in-process `hooks.<name>` op — this handler just
calls each one directly and reads its returned envelope dict, no stdin/
stdout capture, no dynamic file-path import.

THIS IS HOSTING ONLY, NEVER A POLICY CHANGE (unchanged from source): same
three guards, same deny/allow shapes, same fail-open/fail-closed contracts
each guard's own module already states. This op adds zero judgement of its
own.

AGGREGATION CONTRACT — FIRST-DENY-WINS, NOT CONCATENATE-ALL (unchanged from
source). Registration order == DoE's own prior `hooks.json` precedence:

    1. `hooks.block_dispatch_suite_invocation` — NOT YET LANDED in this
       engine (a sibling row's writes:, per the plan's inventory table;
       `docs/plans/2026-09-18-doe-holds-no-scripts.md` line 2227). Dialled
       by NAME through `coordinator_core.ipc.dispatch_local` (see
       `_call_guard_op` below) rather than a direct module import, so this
       fan-in degrades to "leg absent, skip" today and starts firing the
       moment that sibling row lands — no edit to this file required, same
       shape as `enforce_agent_dispatch_mode`'s own reuse of already-landed
       `support.*` modules.
    2. `hooks.block_unenumerated_agent_type` (this row's own sibling op,
       registered in this same commit).
    3. `hooks.enforce_agent_dispatch_mode` (this row's own sibling op) — the
       ONLY leg that ever emits an "allow"+`updatedInput` rewrite; every
       other leg here only ever emits a deny or nothing. Reached only once
       legs 1-2 have all declined to deny, preserving the single-emitter
       invariant automatically: legs 1-2 run first, first-deny-wins,
       short-circuiting leg 3 too.

Retired 2026-09-26 (docs/plans/2026-09-26-retire-review-integrator.md, row
M1): `hooks.guard_review_integrator_sidecar_intake` — the review-integrator
agent it gated is gone, and its sidecar-intake shape has no successor
gate.

FAILURE ISOLATION (unchanged from source): legs 1-2 each run inside their
own `try`/`except BaseException` — one leg crashing (or, for leg 1, being
absent from this engine's op registry) skips only that leg, with a
best-effort stderr breadcrumb, and this dispatcher proceeds to the next.
Leg 3 (`enforce_agent_dispatch_mode`) is NOT wrapped the same way — it is
the sole `updatedInput` emitter and its own handler contract already
degrades every internal failure to `no_advisory()` rather than raising (see
that module's own "Fail-open discipline" section), so an exception reaching
this dispatcher from leg 3 is a genuine bug in that op, not a runtime
condition this fan-in papers over.

Op contract: `params` is the flat PreToolUse payload dict. Returns the
first-firing deny envelope, leg 4's own envelope (deny or rewrite), or
`no_advisory()` when nothing fires.

Negative-spec:
    Does NOT call `guard_named_dispatch_tool_restriction` or
    `nudge_foreground_agent_dispatch` — both are DEREGISTERED from the live
    Agent-matcher fan-in (folded into `enforce_agent_dispatch_mode`'s own
    Concerns F/G); calling them here too would reintroduce the exact
    parallel-emitter race the fold-in closed. Both stay independently
    invocable as their own `hooks.<name>` doors (this row's own sibling
    ops), never as legs of this dispatcher.
    Does NOT import `block_dispatch_suite_invocation` at THIS module's own
    import time — the import is deferred to inside leg 1's own call
    (`_call_suite_invocation_leg`), inside its own `try`/`except
    BaseException`, so a not-yet-landed sibling module degrades to "leg
    absent, skip" for that call rather than an `ImportError` reaching a
    caller who merely imported this dispatcher.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C8
"""

from __future__ import annotations

import sys
from typing import List

from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op

def _is_deny(envelope) -> bool:
    if not isinstance(envelope, dict):
        return False
    hso = envelope.get("hookSpecificOutput")
    return isinstance(hso, dict) and hso.get("permissionDecision") == "deny"


def _call_suite_invocation_leg(params: dict) -> dict:
    from coordinator_core.hooks.block_dispatch_suite_invocation import _handler

    result = _handler(params)
    if not isinstance(result, dict):
        return {}
    return result


def _call_unenumerated_agent_type_leg(params: dict) -> dict:
    from coordinator_core.hooks.block_unenumerated_agent_type import _handler

    return _handler(params)


def _call_enforce_dispatch_mode_leg(params: dict) -> dict:
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    return _handler(params)


@register_op("hooks.preuse_agent_dispatch")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)

    skipped: List[str] = []

    for leg_name, leg_fn in (
        ("block_dispatch_suite_invocation", _call_suite_invocation_leg),
        ("block_unenumerated_agent_type", _call_unenumerated_agent_type_leg),
    ):
        try:
            out = leg_fn(params)
        except BaseException:
            skipped.append(leg_name)
            continue
        if _is_deny(out):
            if skipped:
                try:
                    print(
                        "[preuse_agent_dispatch] guard(s) skipped (fail-open "
                        "for those only): " + ", ".join(skipped),
                        file=sys.stderr,
                    )
                except Exception:
                    pass
            return out

    if skipped:
        try:
            print(
                "[preuse_agent_dispatch] guard(s) skipped (fail-open for "
                "those only): " + ", ".join(skipped),
                file=sys.stderr,
            )
        except Exception:
            pass

    out = _call_enforce_dispatch_mode_leg(params)
    if not isinstance(out, dict):
        return no_advisory()
    return out
