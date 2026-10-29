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

#: Seconds the request waited for a pool worker before the handler ran; declared by `set_queue_wait`.
_QUEUE_WAIT: ContextVar[float] = ContextVar("land_run_queue_wait", default=0.0)

#: Per-scope ledger of started legs as `(leg, start)`; None outside a scope.
_LEGS: ContextVar[Optional[list]] = ContextVar("land_run_legs", default=None)

#: The server's result window (seconds from submit) the request ran under; None when unbound.
_SERVER_WINDOW: ContextVar[Optional[float]] = ContextVar("land_run_server_window", default=None)


def set_queue_wait(secs: float, window_secs: Optional[float] = None) -> None:
    """Declare the seconds this request waited for a pool worker, for `halt_reply` to report, and
    the server's result window it must finish inside. The window is handed in because the ops
    layer must not import the server; with no wait or no window no server-window clamp applies."""
    _QUEUE_WAIT.set(max(0.0, float(secs)))
    _SERVER_WINDOW.set(None if window_secs is None else float(window_secs))


class LandDeadlineExceeded(BaseException):
    """The land_run deadline passed before leg `leg` started."""

    def __init__(self, leg: str, legs: Optional[list] = None) -> None:
        super().__init__(leg)
        self.leg = leg
        self.legs = legs or []


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
    window = _SERVER_WINDOW.get()
    wait = _QUEUE_WAIT.get()
    if window is not None and wait > 0.0:
        server = _now() + max(0.0, window - wait - dispatch_abandon.REPLY_MARGIN_SECS)
        deadline = server if deadline is None else min(deadline, server)
    current = _DEADLINE.get()
    if current is not None and (deadline is None or current < deadline):
        deadline = current
    token = _DEADLINE.set(deadline)
    legs_token = _LEGS.set([])
    try:
        yield
    finally:
        _LEGS.reset(legs_token)
        _DEADLINE.reset(token)


def expired() -> bool:
    """True once the bound deadline has passed; False when none is bound."""
    deadline = _DEADLINE.get()
    return deadline is not None and _now() >= deadline


def check(leg: str) -> None:
    """Raise `LandDeadlineExceeded(leg)` when the deadline has passed, carrying the started legs'
    elapsed; otherwise record the leg's start. A no-op when no deadline is bound."""
    now = _now()
    if expired():
        raise LandDeadlineExceeded(leg, _legs_elapsed(now))
    ledger = _LEGS.get()
    if ledger is not None:
        ledger.append((leg, now))


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
        "legs": list(exc.legs),
    }
