"""Abandonment signal from the dispatcher to a sync op handler running in a worker thread.

`asyncio.wait_for` abandons the AWAIT, never the thread (ipc.py, AC-3), so a handler that
outlives its budget would otherwise land its irreversible step after the caller was told the
op timed out. The dispatcher binds an Event here before offloading the handler and sets it on
timeout; the handler checks `is_abandoned()` immediately before that step.

Invariant: the check is advisory and only guards steps not yet taken. A step already taken is
reported by the handler's own result, never rolled back. Stdlib-only: this module sits on the
cold-start import path.
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import Optional

ACTIVE: ContextVar[Optional[threading.Event]] = ContextVar("dispatch_abandoned", default=None)

# time.monotonic() instant after which the dispatcher stops waiting for this handler's reply.
# The deadline is the dispatcher's wait, not a promise that the handler will be stopped.
DEADLINE: ContextVar[Optional[float]] = ContextVar("dispatch_deadline", default=None)


def remaining() -> Optional[float]:
    """Seconds left before the dispatcher stops waiting for this handler; None when unbound
    (cold in-process path), which a caller treats as unbounded."""
    deadline = DEADLINE.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


# Seconds a deadline-bounded step leaves unspent so its reply still arrives before the backstop:
# serialising and returning the reply, plus the skew between the server's pool wait (starts at
# accept, warm.server._POOL_RESULT_DEADLINE_SECS) and this deadline (starts at bind).
REPLY_MARGIN_SECS = 2.0


def is_abandoned() -> bool:
    ev = ACTIVE.get()
    return ev is not None and ev.is_set()


_live_lock = threading.Lock()
_live_events: set = set()
_HANDLER_DONE_ATTR = "handler_done"


def note_abandoned_mutating(ev: threading.Event) -> Optional[int]:
    """Count the sync handler thread bound to `ev` as live-after-abandonment.

    Returns the live count after the increment, or None when that thread already finished
    (a handler that exits between the budget lapsing and this call leaks nothing)."""
    with _live_lock:
        if getattr(ev, _HANDLER_DONE_ATTR, False):
            return None
        _live_events.add(ev)
        return len(_live_events)


def handler_finished() -> None:
    """Called from a sync handler thread's own `finally`: retire its abandoned-live count, or
    mark its Event done so a later `note_abandoned_mutating` ignores it."""
    ev = ACTIVE.get()
    if ev is None:
        return
    with _live_lock:
        _live_events.discard(ev)
        setattr(ev, _HANDLER_DONE_ATTR, True)


def abandoned_live_count() -> int:
    """Mutating handler threads still running after their dispatch was abandoned."""
    with _live_lock:
        return len(_live_events)
