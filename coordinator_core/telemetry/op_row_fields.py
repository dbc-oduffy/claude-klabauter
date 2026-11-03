"""Extra per-row telemetry fields attached by a handler and drained by the dispatcher.

`begin()` binds a fresh mutable dict to a contextvar; `attach(**fields)` mutates it;
`drain(token)` returns it and resets the var.

Trap: `asyncio.to_thread` runs a sync handler in a COPY of the caller's context, so a
contextvar set inside the handler is invisible to the caller. Only mutation of the dict
bound before the hop crosses back, so call `begin()` before the hop, never in the handler.
"""

from __future__ import annotations

import contextvars
from typing import Any

_fields: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "op_row_fields", default=None
)


def begin() -> contextvars.Token:
    """Bind a fresh field dict; pass the token to `drain`."""
    return _fields.set({})


def attach(**fields: Any) -> None:
    """Merge `fields` into the bound dict; a no-op when none is bound."""
    bound = _fields.get()
    if bound is not None:
        bound.update(fields)


def drain(token: contextvars.Token) -> dict[str, Any]:
    """Return the attached fields and unbind the var."""
    bound = _fields.get() or {}
    _fields.reset(token)
    return bound
