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
from contextvars import ContextVar
from typing import Optional

ACTIVE: ContextVar[Optional[threading.Event]] = ContextVar("dispatch_abandoned", default=None)


def is_abandoned() -> bool:
    ev = ACTIVE.get()
    return ev is not None and ev.is_set()
