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
    "refusal": (MECHANICAL, "wait-out-transient-refusal"),
    "dependents_unstarted": (MECHANICAL, "halt-record-carries-refire-next-action"),
    "split": (DECISION, None),
    "research": (DECISION, None),
    "design-review": (DECISION, None),
    "parked": (MECHANICAL, "halt-record-carries-refire-next-action"),
    # ask_compose terminal halts.
    "usage_limit": (DECISION, None),
    "review_blocked": (DECISION, None),
    "criterion_not_met": (DECISION, None),
    "delivery_fail": (DECISION, None),
    "objective_calibration": (DECISION, None),
    "objective_impossible": (DECISION, None),
    "objective_escalated": (DECISION, None),
    # review and landing: a real conflict or a recorded-only outcome.
    "review_floor": (DECISION, None),
    "peer-contested": (DECISION, None),
    "deleted": (DECISION, None),
    "out-of-worktree": (DECISION, None),
    "halt-record-only": (MECHANICAL, "refire-resumes-kept-work"),
    "halt-on-run-owned-merge": (DECISION, None),
    "halted-run": (MECHANICAL, "refire-resumes-kept-work"),
    "review-reverify-failed": (MECHANICAL, "landing-runs-targeted-retest"),
    "review-reverify-indeterminate": (MECHANICAL, "landing-runs-targeted-retest"),
    "indeterminate": (MECHANICAL, "landing-runs-targeted-retest"),
    "test-verdict-names-no-failure": (MECHANICAL, "landing-runs-targeted-retest"),
    # Mechanical: the remedy is computed into halted_by.next_action, so the run executes it.
    "REGISTER-ROWS-UNCLAIMED": (MECHANICAL, "replan-claim-omitted-rows"),
    "arm-no-replan-leg": (MECHANICAL, "replan-claim-omitted-rows"),
    "targeted-tests-failed": (MECHANICAL, "landing-runs-targeted-retest"),
    "stale-retest-freshness": (MECHANICAL, "retest-fresh-by-blob"),
    "live-briefed-revise-refused": (MECHANICAL, "replan-delta-rows-in-run"),
    "pre-existing-dirt-own-sizing": (MECHANICAL, "adopt-own-sizing-residue"),
    "own-path-revert-blocked": (MECHANICAL, "admit-own-path-revert"),
    "review_blocked-designed_red": (MECHANICAL, "honour-designed-red-marker"),
    # ask_compose JS halts: the halted record carries the computed revise-and-refire next_action, so the halt
    # commit keeps the run's work and the refire resumes it.
    "not_closeable": (MECHANICAL, "halt-record-carries-refire-next-action"),
    "stop_rule": (MECHANICAL, "halt-record-carries-refire-next-action"),
    # From the halt census: converting sites call remedy_or_halt under these kinds and never write this table.
    "accepted-plan-exists": (MECHANICAL, "bind-accepted-plan-and-resume"),
    "plan-exists-stage1-overwrite": (MECHANICAL, "resume-existing-plan"),
    "plan-already-ran": (MECHANICAL, "route-all-landed-to-verify"),
    "unread-revise-notes-on-ran": (MECHANICAL, "reopen-on-unread-revise-notes"),
    "plan-not-decent": (MECHANICAL, "upgrade-plan-in-run"),
    "run-token-spent": (MECHANICAL, "mint-new-run-token-and-refire"),
    "orphan-pending-unadopted": (MECHANICAL, "adopt-orphan-pending-run"),
    "baton-not-this-sizings": (MECHANICAL, "rebind-baton-from-sizing"),
    "anchor-falsified-by-own-landed-rows": (MECHANICAL, "reanchor-to-post-conversion-check-pinned-to-baseline-ref"),
    # The dry-refire publish gate replays a built-in sample.
    "dry-refire-no-refireable-run": (MECHANICAL, "replay-built-in-sample"),
    # A halt kind with no constant of its own (orphan_adopt.ORPHAN_PENDING); the census classes it recoverable.
    "orphan_pending": (MECHANICAL, "adopt-orphan-pending-run"),
}

#: Class label of a kind whose continuing would do irreversible harm or commit an unasked direction.
#: These kinds stay DECISION rows in HALT_REMEDY (the shape guard pins that table's two classes).
IRREVERSIBLE = "irreversible"

#: kind -> the harm that makes continuing wrong. A halt of one of these kinds may reach the EM.
IRREVERSIBLE_KINDS: dict[str, str] = {
    # Direction-class: only the PM/APM can answer; proceeding unasked spends the run on the wrong scope.
    "room": "route is room-owned: scope/shape needs the PM or G-EM",
    "touchpoint": "exit criterion unaccepted: a run without one has no stop to be judged against",
    "split": "xl_exit split: the bundle becomes sibling runs, a scope decision",
    "research": "research sizing: the fire entry, not this run, owns it",
    "design-review": "design approval is the PM's recorded ruling",
    "objective_calibration": "objective calibration needs the PM's ruling",
    "objective_impossible": "objective judged impossible: continuing spends on an unreachable end",
    "objective_escalated": "objective escalated to the PM",
    # External or terminal-verdict: the halt is the run's true end state.
    "usage_limit": "third-party quota exhausted: no engine remedy exists",
    "review_blocked": "review blocked the code: landing it is shipping rejected work",
    "review_floor": "review floor unmet: landing it is shipping unreviewed work",
    "criterion_not_met": "exit criterion unmet: continuing claims a false completion",
    "delivery_fail": "delivery failed: the deliverable is not deliverable",
    # Data-loss / clobber.
    "peer-contested": "overwrites a peer's uncommitted edit: data loss",
    "deleted": "restores or removes a deleted path: data loss",
    "out-of-worktree": "writes outside the worktree",
    "halt-on-run-owned-merge": "resolves a merge over run-owned history unasked: history rewrite",
}

#: kind -> the PM ruling that keeps it a refusal. Never a recoverable debt; the stop_after refusal is the only one.
PM_RULED_KEEPER: dict[str, str] = {
    "stop_after-refusal": (
        "docs/plans/2026-10-28-refuse-stop-after-in-engine-ask.md: PM, 'why would we want to support bringing "
        "back in the behaviors that workflows are there to prevent?'"
    ),
}

#: Recoverable DECISION kinds, kind -> emitting module. Empty: a new recoverable halt kind is MECHANICAL above.
RECOVERABLE_DEBT: dict[str, str] = {}


def is_mechanical(kind: str) -> bool:
    """True when `kind` is classified mechanical; an unclassified kind is False (the guard test refuses it)."""
    return HALT_REMEDY.get(kind, (DECISION, None))[0] == MECHANICAL


def is_irreversible(kind: str) -> bool:
    """True when `kind` is in IRREVERSIBLE_KINDS: a halt of it may reach the EM."""
    return kind in IRREVERSIBLE_KINDS


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
