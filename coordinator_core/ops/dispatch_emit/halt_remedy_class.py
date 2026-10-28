"""Registry of halt kinds: which may reach the EM (decision) and which the run remedies itself (mechanical).

A mechanical kind's remedy is already computed by the engine; the run performs it through `remedy_or_halt`
and halts only once the bounded attempts are spent. `tests/test_no_mechanical_halt_reaches_em.py` fails when a
mechanical kind still has an emitting site outside `remedy_or_halt`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

DECISION = "decision"
MECHANICAL = "mechanical"

#: In-run tries before a mechanical remedy's failure is a real failure the EM sees.
MAX_IN_RUN_REMEDY_ATTEMPTS = 2

#: kind -> (class, in-run remedy name). A decision kind carries no remedy (None).
#: Keys are the kind strings the halt sites emit; pseudo-kinds name a condition with no constant of its own.
HALT_REMEDY: dict[str, tuple[str, Optional[str]]] = {
    # ask_contract: the run needs a human or G-EM answer.
    "room": (DECISION, None),
    "touchpoint": (DECISION, None),
    "refusal": (DECISION, None),
    "dependents_unstarted": (DECISION, None),
    "split": (DECISION, None),
    "research": (DECISION, None),
    "design-review": (DECISION, None),
    "parked": (DECISION, None),
    # ask_compose terminal halts.
    "usage_limit": (DECISION, None),
    "review_blocked": (DECISION, None),
    "criterion_not_met": (DECISION, None),
    "delivery_fail": (DECISION, None),
    "not_closeable": (DECISION, None),
    "stop_rule": (DECISION, None),
    "objective_calibration": (DECISION, None),
    "objective_impossible": (DECISION, None),
    "objective_escalated": (DECISION, None),
    # review and landing: a real conflict or a recorded-only outcome.
    "review_floor": (DECISION, None),
    "peer-contested": (DECISION, None),
    "deleted": (DECISION, None),
    "out-of-worktree": (DECISION, None),
    "halt-record-only": (DECISION, None),
    "halt-on-run-owned-merge": (DECISION, None),
    "halted-run": (DECISION, None),
    "review-reverify-failed": (DECISION, None),
    "review-reverify-indeterminate": (DECISION, None),
    "indeterminate": (DECISION, None),
    "test-verdict-names-no-failure": (DECISION, None),
    # Mechanical: the remedy is computed into halted_by.next_action, so the run executes it.
    "REGISTER-ROWS-UNCLAIMED": (MECHANICAL, "replan-claim-omitted-rows"),
    "arm-no-replan-leg": (MECHANICAL, "replan-claim-omitted-rows"),
    "targeted-tests-failed": (MECHANICAL, "landing-runs-targeted-retest"),
    "stale-retest-freshness": (MECHANICAL, "retest-fresh-by-blob"),
    "live-briefed-revise-refused": (MECHANICAL, "replan-delta-rows-in-run"),
    "pre-existing-dirt-own-sizing": (MECHANICAL, "adopt-own-sizing-residue"),
    "own-path-revert-blocked": (MECHANICAL, "admit-own-path-revert"),
    "review_blocked-designed_red": (MECHANICAL, "honour-designed-red-marker"),
}


def is_mechanical(kind: str) -> bool:
    """True when `kind` is classified mechanical; an unclassified kind is False (the guard test refuses it)."""
    return HALT_REMEDY.get(kind, (DECISION, None))[0] == MECHANICAL


def remedy_or_halt(
    kind: str,
    attempt_fn: Callable[[int], Any],
    halt_fn: Callable[[Sequence[str]], Any],
    attempts: int = MAX_IN_RUN_REMEDY_ATTEMPTS,
) -> Any:
    """Run `attempt_fn(n)` up to `attempts` times; return its first non-raising result.

    Every halt site of a mechanical `kind` calls this. `halt_fn(failures)` runs only when the bound is spent,
    with one message per failed attempt, and its result (or exception) is the caller's halt.
    """
    if not is_mechanical(kind):
        raise ValueError(f"remedy_or_halt: {kind!r} is not a mechanical halt kind")
    failures: list[str] = []
    for n in range(1, max(1, attempts) + 1):
        try:
            return attempt_fn(n)
        except Exception as exc:  # a failed attempt is data for the halt record, not a crash
            failures.append(f"attempt {n}: {type(exc).__name__}: {exc}")
    return halt_fn(failures)
