"""coordinator_core.op_budget_suspension — the ops that are OFF for blowing the 2s bar.

Purpose: stop an over-budget op from firing at all, rather than granting it a
larger timeout. A suspended op is refused at dispatch, before its owning module
is imported, so a breach costs the box nothing — not the work, not the import.

PM ruling, 2026-08-21, verbatim: *"everything taking over 2s max should be turned
off. turning them back on, each of them needs to earn it. nothing is worth >2s,
especially now we have a warm engine. so, off now, to save the box, and stop this
bullshit where they get paper cuts."*

The bar is MAX, not p50 or p95. An op whose median is fast and whose worst case is
56 seconds has still occupied the box for 56 seconds, and at this machine's load
norm (~50 concurrent sessions) that is 50 peers held behind one invocation. A
distribution argument is exactly the "paper cuts" move the ruling names: every
individual breach is defensible in isolation and the box still dies of them.

Why suspension and not a lowered timeout: a timeout that fires still paid for the
work up to the bound, and the op comes back next invocation to pay again. A dial
lowered to 2s converts a 30s breach into fifteen 2s breaches. Off is the only
disposition that returns the time.

REINSTATEMENT — the whole point of the table, and the only path back:
    An op earns its way back by PROVING it runs under 2s WITHOUT the warm engine
    — cold interpreter, cold caches, under load — and the PM admits it to the
    candidate build on klabauter. Warm-engine numbers do not discharge this: the
    warm engine is what makes an over-budget op survivable, and reinstating on
    warm numbers reinstates the defect with its evidence attached. Each op has a
    spinoff carrying its own reinstatement case; `spinoff` names it.

    SPAWN COUNT IS THE LOAD-INVARIANT FIGURE, AND IT IS REQUIRED.
    A wall-clock number taken under load is mostly a measurement of this session's
    peers, not of the op. Measured on this box, 2026-08-21, same three git reads
    taken twice: in-process 31ms under load vs 30ms on a quiet box — flat; three
    subprocess spawns 3,188ms under load vs 158ms quiet — a 20x swing. A single
    `git --version` ranged 37ms to 1,394ms with ~0ms CPU either way. The cost is
    process creation, not the work.

    So a reinstatement case that reports only wall time is not reproducible: the
    same code passes at 3am and fails at noon. Every case MUST report spawn count
    and process time alongside any wall figure, and the spawn count is the number
    that decides it. This restates CLAUDE.md § brightline ("process time and spawn
    count, never wall clock") at the seam where the temptation to ignore it is
    highest — the moment someone wants their op back.

    AND REPORT n WITH QUANTILES, NEVER A SINGLE SAMPLE.
    Measured here at n=30: `git --version` — the most trivial control that exists,
    doing no work whatsoever — runs 15.3ms at min, 17.5ms at p50, and 279.3ms at
    p99. A ~20x spread on nothing. One sample on this box measures the peer fleet,
    not the code.

    That is not hypothetical: five figures in the campaign that produced this table
    failed this way and were withdrawn. Three rules survive them, and they are the
    contract this docstring owes a reader:

        Re-run it — a single sample on a box with real spread is not a result.
        Re-open it — having once seen a number is not evidence, and a scratch copy
            of one is not a source. This applies hardest to a figure relayed from
            somebody else, which arrives looking checked precisely because it cites.
        Prefer a control — holding conditions constant beats measuring carefully
            under conditions you do not control.

    The campaign narrative behind those rules — which five figures, how each failed,
    which were caught by their own author and which by a reader — is a dated record,
    not a contract, and lives in
    `state/2026-08-21-ops-turned-off-for-the-2s-max-bar.md` and
    `state/lessons/2026-08-21-a-relayed-number-is-not-a-checked-number.yaml`. Those
    numbers go stale; the three rules do not.

    So: p50 and p90 over a real n, or the case is not made. And prefer a CONTROL to
    a sample wherever one is available — the strongest evidence this campaign
    produced was 160s-through-the-op against 8.9s-through-plain-git-commit, same
    paths, same branch, same minute. Holding conditions constant beats measuring
    more carefully under conditions you do not control.

    The `measured` numbers in this table are wall-clock occupancy from the
    op-latency sink, and they are stated as such. That is the correct measure for
    what this table is FOR — an op holding the box for 30s holds it for 30s
    whatever the reason — and it is the wrong measure for deciding whether a
    rebuild succeeded. Do not reuse these numbers as a reinstatement baseline
    without converting to spawn count first.

Negative-spec:
    - This table SHRINKS, never grows by tuning. An op leaves it by being made
      fast or by being deleted, never by being re-measured on a quiet box.
    - No environment variable, parameter, or envelope field lifts a suspension.
      There is deliberately no argument this module accepts that changes its
      answer — the knob that would be the escape hatch does not exist to be found.
    - BOTH invocation doors are guarded, not just the front one. `ipc`'s JSON-RPC
      dispatch refuses with `-32006`; `ipc.get_op_handler` raises
      `OpSuspendedError`. The second is not redundant: resolving a handler by key
      and awaiting it directly ("path 3", see `warm/entry_seam.py`) is a dozen-site
      production pattern here, and guarding only dispatch left the roster's two
      most expensive ops fully reachable — `safe_commit_offer.py :: _commit_group`
      resolves `ceremony.scoped_git_commit`, `tail_ops.py` resolves
      `review_trail.write`. A guard on the door people do not use is not a guard.
    - Refusal happens BEFORE registry lookup and therefore before the lazy import
      of the op's owning module. Refusing after the import would still pay the
      import cost, which for several of these ops is the cheap part of a very
      expensive call, but is not nothing at 50 concurrent sessions.
    - The refusal message never names a bypass, and never offers "raise the
      timeout" as the alternative. There is no shape of this message in which
      that text is correct. Conforms to docs/wiki/guard-messaging.md § Register.
    - `measured` is EVIDENCE, not a threshold. Nothing reads these numbers to
      make a decision; they exist so the refused caller learns why without going
      to look, and so a reinstatement case has a baseline it must beat.
    - Membership is by exact op name. No prefix matching: unlike the ceremony
      budget, this is not a class of ops that should be born inside a rule, it is
      a finite list of specific defects, and every entry is meant to leave.

Spec backlink: docs/decisions/DR-349-one-budget-governs-every-constructed-op.md
               docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md
               state/sizings/2026-08-21-lower-the-global-op-budget-to-2s-and-ado.yaml
"""

from __future__ import annotations

from typing import Dict, Optional

__all__ = [
    "SUSPENSION_BAR_MS",
    "SUSPENDED_OPS",
    "OpSuspendedError",
    "is_suspended",
    "suspension_record",
    "refusal_message",
]


class OpSuspendedError(RuntimeError):
    """Raised when a suspended op is resolved for IN-PROCESS invocation.

    The JSON-RPC dispatch path refuses a suspended op with `-32006` and never
    reaches this. This exists for the OTHER way an op gets invoked in this
    codebase: `ipc.get_op_handler(name)` returning a callable the caller then
    awaits directly — "path 3" re-entry, formalised in `warm/entry_seam.py` and
    used from a dozen production sites. Refusing only at dispatch left the two
    most expensive ops in the roster reachable at full cost through that door
    (`safe_commit_offer.py :: _commit_group` resolves `ceremony.scoped_git_commit`;
    `tail_ops.py` resolves `review_trail.write`).

    Why an exception rather than returning None: `get_op_handler`'s contract is
    that None means "genuinely unregistered", and callers branch on that as a
    not-found condition. A suspended op is registered and would work — returning
    None would make it indistinguishable from a missing op and let a caller
    degrade past it silently, which is the opposite of the ruling's intent that
    the failure be visible. Loud is the point.
    """


# The PM's bar, in milliseconds, measured as MAX end-to-end per invocation.
# This number may be LOWERED, never raised — DR-344's brightline is 500ms and this
# is already four times more generous than the target it serves. The ratchet test
# `coordinator_core/tests/test_op_suspension_ratchet.py` fails on any edit lifting it.
SUSPENSION_BAR_MS = 2000.0


# Measured 2026-08-21 by `op_census.breaches` (top_n=null) over the current
# op-latency generation, corroborated by a direct full-generation read. Every row
# here blew SUSPENSION_BAR_MS on MAX. Ops that breached DR-344's 500ms brightline
# but stayed under 2s on max are deliberately ABSENT — they are defects, but they
# are not box-occupying defects, and this table is the box's defence, not the
# brightline's enforcement.
#
# The four `test.*` fixtures that breach by construction are also absent, and not
# by carve-out: their max latencies (66-110ms) simply fit. A rule that needed an
# exception for its own test fixtures would be the wrong rule.
SUSPENDED_OPS: Dict[str, Dict[str, object]] = {
    "ceremony.scoped_git_commit": {
        "measured": {"max_ms": 150021.1, "p50_ms": 30694.6, "n": 338},
        "note": "Rode the 150s ceiling. Hourly p50 degraded 3-8s -> 85.4s over 2026-08-21.",
        "spinoff": None,
    },
    "ceremony.wsc_tail": {
        "measured": {"max_ms": 220191.7, "p50_ms": 30015.6, "n": 12},
        "note": "Worst single occupancy measured anywhere in the tree: 220s.",
        "spinoff": None,
    },
    "session.sweep_consumed_handoffs": {
        "measured": {"max_ms": 104963.8, "p50_ms": 30193.9, "n": 2},
        "note": "The 30s ceiling is the operating point, not the tail.",
        "spinoff": None,
    },
    "artifact.emit": {
        "measured": {"max_ms": 56645.9, "p50_ms": 1.6, "n": 43},
        "note": "p50 1.6ms. The median proves nothing; the 56s max is the box damage.",
        "spinoff": None,
    },
    "probes.fork_census": {
        "measured": {"max_ms": 42367.5, "p50_ms": 42367.5, "n": 1},
        "note": "Single invocation, 42s. Every invocation observed was a breach.",
        "spinoff": None,
    },
    "session.boot_sweep": {
        "measured": {"max_ms": 30016.6, "p50_ms": 30010.8, "n": 8},
        "note": "8/8 ended in caller_timeout at 30s.",
        "spinoff": None,
    },
    "queue.close": {
        "measured": {"max_ms": 30016.9, "p50_ms": 30016.9, "n": 1},
        "note": "Ran to the ceiling on its only observed invocation.",
        "spinoff": None,
    },
    "memo.send": {
        "measured": {"max_ms": 30015.7, "p50_ms": 7133.5, "n": 20},
        "note": "94% breach rate.",
        "spinoff": None,
    },
    "fleet.archive_completed_plans": {
        "measured": {"max_ms": 27940.0, "p50_ms": 807.3, "n": 3},
        "spinoff": None,
    },
    "fleet.archive_completed_handoffs": {
        "measured": {"max_ms": 26111.9, "p50_ms": 26111.9, "n": 1},
        "spinoff": None,
    },
    "review_trail.write": {
        "measured": {"max_ms": 16558.8, "p50_ms": 3986.3, "n": 108},
        "note": "96% breach rate over 108 invocations.",
        "spinoff": None,
    },
    "fleet.archive_actioned_memos": {
        "measured": {"max_ms": 13054.5, "p50_ms": 10547.4, "n": 4},
        "spinoff": None,
    },
    "op_census.report": {
        "measured": {"max_ms": 11734.5, "p50_ms": 1662.1, "n": 3},
        "note": "Subject to the bar it reports on.",
        "spinoff": None,
    },
    "completion.reconcile_commits": {
        "measured": {"max_ms": 7865.0, "p50_ms": 3374.3, "n": 26},
        "note": "26/26 breached.",
        "spinoff": None,
    },
    "hooks.cater_subagent_start": {
        "measured": {"max_ms": 6950.8, "p50_ms": 1415.6, "n": 247},
        "note": "Fires on every subagent dispatch; 90% breach rate.",
        "spinoff": None,
    },
    "hooks.track_touched_files": {
        "measured": {"max_ms": 6939.7, "p50_ms": 11.1, "n": 3439},
        "note": "Fires on every edit. 3439 invocations, trend worsening.",
        "spinoff": None,
    },
    "testing.full_runner": {
        "measured": {"max_ms": 4919.1, "p50_ms": 1963.3, "n": 2},
        "note": "DR-349 names the test runner a carve-out; the carve-out is for the "
                "RUN's duration, not for this op's own dispatch overhead.",
        "spinoff": None,
    },
}


def is_suspended(method: object) -> bool:
    """True when *method* is an op that has been turned off for blowing the bar."""
    return isinstance(method, str) and method in SUSPENDED_OPS


def suspension_record(method: str) -> Optional[Dict[str, object]]:
    """The measured evidence behind *method*'s suspension, or None if it is live."""
    return SUSPENDED_OPS.get(method)


def refusal_message(method: str) -> str:
    """The caller-facing refusal for a suspended op.

    Register (docs/wiki/guard-messaging.md): one fact, once, then a terse
    imperative. The alternative is the reinstatement bar — cold, not warm — and
    never a way around the refusal, because there is not one.
    """
    record = SUSPENDED_OPS.get(method)
    max_ms = 0.0
    if isinstance(record, dict):
        measured = record.get("measured")
        if isinstance(measured, dict):
            try:
                max_ms = float(measured.get("max_ms") or 0.0)
            except (TypeError, ValueError):
                max_ms = 0.0
    return (
        f"{method} is off: measured max {max_ms:.0f}ms against a "
        f"{SUSPENSION_BAR_MS:.0f}ms bar. Prove it under "
        f"{SUSPENSION_BAR_MS / 1000:.0f}s without the warm engine to bring it back."
    )
