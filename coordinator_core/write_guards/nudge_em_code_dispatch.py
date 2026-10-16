"""coordinator_core.write_guards.nudge_em_code_dispatch — advisory guard.

Fan-in port of the retired DoE PreToolUse(Write|Edit|MultiEdit) shim
`coordinator/hooks/scripts/nudge-em-code-dispatch.py`, which was itself a
thin stdin->stdout trampoline over this same module's `op()` function
(`coordinator_core.hooks.nudge_em_code_dispatch`). Per the porting contract
(`write_guards/INTERFACE.md`) and the wave map's C9 slot this is a
smallest-full-port: the guard interface (`check(payload)`) wraps
the existing `op(payload)` call rather than re-implementing or moving its
logic. `op()` already contains the full line-for-line port of the JS
predecessor (bootstrap/out-of-repo carve-out, executor-type derivation,
pending-dispatch artifact, sentinel suppression) — nothing here duplicates
that.

CLASS/MATCHERS/PRIORITY convention per docs/wiki/write-guard-priority-bands.md
(the real SSOT -- the prior citation, state/plan-sidecars/2026-07-29-hook-
fan-in-write-path.priority-map.md, named a file that was never written).

CLASS is advisory: for the main EM this guard offers a better path via
`additionalContext` and does not deny it. MATCHERS is `Write|Edit|MultiEdit` only —
NOT `NotebookEdit` — taken from the live `hooks.json` registration for
`nudge-em-code-dispatch.py` (idx 3), not from INTERFACE.md's illustrative
four-matcher example. PRIORITY is 105 per the wave map's assigned slot
(just below the lowest pre-existing advisory, reflecting this guard's
pre-fold position as the earliest-running advisory process).

Subagent writes (payload carries `agent_id`) are never advised. They are
denied in exactly one case: the subagent is a spawn of a live warp run that
has bound no sizing (its size stage), attributed by its spawn transcript dir,
and the write is a product file
(`coordinator_core.hooks.nudge_em_code_dispatch._size_stage_write_deny`).
Every other subagent, including a hand-dispatched executor in the same
session, passes. This module re-implements none of that; it delegates to
`op()`.

Fail-open: `op()` is called inside a bare try/except that returns `None` on
any failure, matching the retired shim's own graceful-degradation contract
("any failure ... falls through to fail-open ALLOW ... must NEVER block").
This is the correct shape for an advisory guard (fidelity rule 6) — it does
not convert a fail-closed guard into fail-open, because this guard was never
fail-closed to begin with.

Spec backlink: coordinator-content-repo:pln-hook-fan-in-fold-the-pretoolus-27c1e9 § C9
Source: coordinator/hooks/scripts/nudge-em-code-dispatch.py
"""

from __future__ import annotations

from coordinator_core.hooks.nudge_em_code_dispatch import op as _op

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 105


def check(payload: dict) -> dict | None:
    try:
        result = _op(payload)
    except Exception:
        return None
    return result or None
