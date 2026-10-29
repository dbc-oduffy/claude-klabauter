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

REFUSED = "land-deadline"

#: Monotonic clock seam; tests substitute a fake clock so nothing sleeps.
_now = time.monotonic

_DEADLINE: ContextVar[Optional[float]] = ContextVar("land_run_deadline", default=None)


class LandDeadlineExceeded(BaseException):
    """The land_run deadline passed before leg `leg` started."""

    def __init__(self, leg: str) -> None:
        super().__init__(leg)
        self.leg = leg


@contextmanager
def scope(budget_secs: Optional[float] = None) -> Iterator[None]:
    """Bind the op's deadline; an enclosing, earlier deadline (or the dispatcher's own, less its
    reply margin) wins. `budget_secs` of None uses `LAND_BUDGET_SECS`; a `LAND_BUDGET_SECS` of None
    disables the bound."""
    budget = LAND_BUDGET_SECS if budget_secs is None else budget_secs
    deadline = None if budget is None else _now() + budget
    from coordinator_core import dispatch_abandon

    left = dispatch_abandon.remaining()
    if left is not None:
        outer = _now() + max(0.0, left - dispatch_abandon.REPLY_MARGIN_SECS)
        deadline = outer if deadline is None else min(deadline, outer)
    current = _DEADLINE.get()
    if current is not None and (deadline is None or current < deadline):
        deadline = current
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def expired() -> bool:
    """True once the bound deadline has passed; False when none is bound."""
    deadline = _DEADLINE.get()
    return deadline is not None and _now() >= deadline


def check(leg: str) -> None:
    """Raise `LandDeadlineExceeded(leg)` when the deadline has passed; a no-op when none is bound."""
    if expired():
        raise LandDeadlineExceeded(leg)


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
    }
