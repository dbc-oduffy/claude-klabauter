"""One land_run-wide deadline that every read and spawn leg on the landing path checks.

A landing is many small reads with no shared budget, so a storage stall let whichever read came next
consume the whole op timeout. `scope()` binds one monotonic deadline for the op; each leg calls
`check(leg)` before it starts, and a leg past the deadline ends the landing as a named halt
(`refused="land-deadline"`, the leg in the reason). A read already blocked in the kernel cannot be
interrupted (no threads, no signals); the deadline stops the NEXT leg from starting.

Invariants: `LandDeadlineExceeded` is a `BaseException`, so a `except Exception` fail-open in a leg
cannot swallow it and read an unverified check as passed. `check` is called only on legs BEFORE the
ref moves; a leg after the commit uses `expired()` and degrades to a recorded skip, so a landed
commit is never reported as anything but landed.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

#: Seconds one land_run may spend before its next leg halts; well under the 30s op budget.
LAND_BUDGET_SECS = 10.0

#: Seconds past `LAND_BUDGET_SECS` that only the `RESERVED_LEGS` may start in, so a reviewed landing
#: is not refused by a deadline its optional legs spent. Outer clamps (dispatcher, server window)
#: bound both deadlines and stay authoritative.
RESERVE_SECS = 3.0

#: Final name segments of the legs that draw on the reserve (`land_run.integrity`, `*.commit`).
RESERVED_LEGS = frozenset({"integrity", "_integrity", "commit"})

REFUSED = "land-deadline"

#: Monotonic clock seam; tests substitute a fake clock so nothing sleeps.
_now = time.monotonic

_DEADLINE: ContextVar[Optional[float]] = ContextVar("land_run_deadline", default=None)

#: Seconds the request waited for a pool worker before the handler ran; declared by `set_queue_wait`.
_QUEUE_WAIT: ContextVar[float] = ContextVar("land_run_queue_wait", default=0.0)

#: Deadline for the reserved legs (>= `_DEADLINE`); None when unbound.
_HARD: ContextVar[Optional[float]] = ContextVar("land_run_hard_deadline", default=None)

#: Per-scope ledger of started legs as `(leg, start)`; None outside a scope.
_LEGS: ContextVar[Optional[list]] = ContextVar("land_run_legs", default=None)

#: The server's result window (seconds from submit) the request ran under; None when unbound.
_SERVER_WINDOW: ContextVar[Optional[float]] = ContextVar("land_run_server_window", default=None)


#: Which bound set a deadline: the leg budget itself, the dispatcher's remaining time, or the
#: server's result window.
BOUND_BUDGET = "budget"
BOUND_DISPATCHER = "dispatcher"
BOUND_SERVER_WINDOW = "server-window"

#: `(deadline_by, hard_by)` of the innermost scope; None when unbound.
_BOUND_BY: ContextVar[Optional[tuple]] = ContextVar("land_run_bound_by", default=None)

#: Earliest outer clamp (dispatcher, server window) of the innermost scope: no `excepted` extension
#: moves a deadline past it. None when no clamp is bound.
_CEILING: ContextVar[Optional[float]] = ContextVar("land_run_ceiling", default=None)


def set_queue_wait(secs: float, window_secs: Optional[float] = None) -> None:
    """Declare the seconds this request waited for a pool worker, for `halt_reply` to report, and
    the result window, measured from submit, the reply must land inside. The window is handed in
    because the ops layer must not import the server; with no wait or no window no server-window
    clamp applies."""
    _QUEUE_WAIT.set(max(0.0, float(secs)))
    _SERVER_WINDOW.set(None if window_secs is None else float(window_secs))


class LandDeadlineExceeded(BaseException):
    """The land_run deadline passed before leg `leg` started."""

    def __init__(self, leg: str, legs: Optional[list] = None, bound_by: str = BOUND_BUDGET) -> None:
        super().__init__(leg)
        self.leg = leg
        self.legs = legs or []
        self.bound_by = bound_by


@contextmanager
def scope(budget_secs: Optional[float] = None) -> Iterator[None]:
    """Bind the op's deadlines: legs start until `budget_secs`, the reserved legs until `RESERVE_SECS`
    later; an enclosing, earlier deadline (or the dispatcher's own, less its
    reply margin) wins. `budget_secs` of None uses `LAND_BUDGET_SECS`; a `LAND_BUDGET_SECS` of None
    disables the bound."""
    budget = LAND_BUDGET_SECS if budget_secs is None else budget_secs
    deadline = None if budget is None else _now() + budget
    hard = None if deadline is None else deadline + RESERVE_SECS
    from coordinator_core import dispatch_abandon

    clamps = []
    left = dispatch_abandon.remaining()
    if left is not None:
        clamps.append((_now() + max(0.0, left - dispatch_abandon.REPLY_MARGIN_SECS), BOUND_DISPATCHER))
    window = _SERVER_WINDOW.get()
    wait = _QUEUE_WAIT.get()
    if window is not None and wait > 0.0:
        clamps.append(
            (_now() + max(0.0, window - wait - dispatch_abandon.REPLY_MARGIN_SECS), BOUND_SERVER_WINDOW)
        )
    deadline_by = hard_by = BOUND_BUDGET
    for clamp, source in clamps:
        if deadline is None or clamp < deadline:
            deadline, deadline_by = clamp, source
        if hard is None or clamp < hard:
            hard, hard_by = clamp, source
    current = _DEADLINE.get()
    if current is not None and (deadline is None or current < deadline):
        deadline = current
        deadline_by = (_BOUND_BY.get() or (deadline_by, hard_by))[0]
    current_hard = _HARD.get()
    if current_hard is not None and (hard is None or current_hard < hard):
        hard = current_hard
        hard_by = (_BOUND_BY.get() or (deadline_by, hard_by))[1]
    ceiling = min([clamp for clamp, _ in clamps] + [c for c in (_CEILING.get(),) if c is not None], default=None)
    token = _DEADLINE.set(deadline)
    hard_token = _HARD.set(hard)
    bound_token = _BOUND_BY.set((deadline_by, hard_by))
    ceiling_token = _CEILING.set(ceiling)
    legs_token = _LEGS.set([])
    try:
        yield
    finally:
        _LEGS.reset(legs_token)
        _CEILING.reset(ceiling_token)
        _BOUND_BY.reset(bound_token)
        _HARD.reset(hard_token)
        _DEADLINE.reset(token)


@contextmanager
def excepted() -> Iterator[None]:
    """Time spent in this block does not count against the leg budget: both deadlines move later
    by its elapsed, never past the scope's outer clamp. For the landing retest, a bulk test the
    brightline excepts (DR-344 scope); counted, a 9-13s retest left no budget for the legs after it."""
    start = _now()
    try:
        yield
    finally:
        spent = _now() - start
        ceiling = _CEILING.get()
        for var in (_DEADLINE, _HARD):
            current = var.get()
            if current is not None:
                moved = current + spent
                var.set(moved if ceiling is None else min(moved, max(current, ceiling)))


def expired() -> bool:
    """True once the bound budget deadline has passed; False when none is bound."""
    deadline = _DEADLINE.get()
    return deadline is not None and _now() >= deadline


def check(leg: str) -> None:
    """Raise `LandDeadlineExceeded(leg)` when the deadline has passed, carrying the started legs'
    elapsed; otherwise record the leg's start. A no-op when no deadline is bound."""
    now = _now()
    hard = _HARD.get()
    if _is_reserved(leg):
        refused = hard is not None and now >= hard
    else:
        refused = expired()
    if refused:
        by = _BOUND_BY.get() or (BOUND_BUDGET, BOUND_BUDGET)
        raise LandDeadlineExceeded(leg, _legs_elapsed(now), by[1] if _is_reserved(leg) else by[0])
    ledger = _LEGS.get()
    if ledger is not None:
        ledger.append((leg, now))


def _is_reserved(leg: str) -> bool:
    """True for the integrity and commit legs and the sub-legs under them, which may spend the
    reserved tail of the budget."""
    return any(seg in RESERVED_LEGS for seg in leg.split("."))


def _legs_elapsed(now: float) -> list:
    """Each started leg's elapsed: its start to the next leg's start, the last to `now`."""
    ledger = _LEGS.get() or []
    ends = [start for _, start in ledger[1:]] + [now]
    return [
        {"leg": name, "elapsed_secs": max(0.0, end - start)}
        for (name, start), end in zip(ledger, ends)
    ]


def halt_reply(exc: LandDeadlineExceeded) -> dict:
    """The named halt a deadline-ended landing answers: nothing committed, safe to re-fire."""
    return {
        "committed": False,
        "sha": None,
        "error": (
            f"land-deadline: leg {exc.leg} did not start inside the land_run budget; "
            "nothing committed, nothing verified past that leg. Re-fire the landing."
        ),
        "refused": REFUSED,
        "leg": exc.leg,
        "queue_wait_secs": _QUEUE_WAIT.get(),
        "bound_by": exc.bound_by,
        "legs": list(exc.legs),
    }
