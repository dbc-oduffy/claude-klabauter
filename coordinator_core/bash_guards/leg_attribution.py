"""Per-call leg attribution: which legs of one hook call cost CPU over a floor.

`begin()` activates a collector in the calling context; `with leg(name):` adds the
`time.thread_time` delta of its body to `name`; `summary()` names the top legs over a
floor. Inactive, `leg()` returns a shared no-op and allocates nothing.

Trap: the collector lives in a contextvar, so `begin()` and `leg()`/`summary()` must run
in the same thread context (a hop through `asyncio.to_thread` copies the context).
"""

from __future__ import annotations

import contextvars
import time
from typing import Any

_collector: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "leg_attribution_collector", default=None
)


class _NoopLeg:
    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


_NOOP = _NoopLeg()


class _Leg:
    __slots__ = ("_acc", "_name", "_t0")

    def __init__(self, acc: dict[str, float], name: str) -> None:
        self._acc = acc
        self._name = name
        self._t0 = 0.0

    def __enter__(self) -> None:
        self._t0 = time.thread_time()
        return None

    def __exit__(self, *exc: object) -> None:
        delta = time.thread_time() - self._t0
        self._acc[self._name] = self._acc.get(self._name, 0.0) + delta
        return None


def begin() -> contextvars.Token:
    """Activate a fresh collector; pass the returned token to `end`."""
    return _collector.set({})


def end(token: contextvars.Token) -> None:
    """Deactivate the collector `begin` bound."""
    _collector.reset(token)


def active() -> bool:
    return _collector.get() is not None


def leg(name: str) -> Any:
    """Context manager accumulating this body's thread CPU time under `name`."""
    acc = _collector.get()
    if acc is None:
        return _NOOP
    return _Leg(acc, name)


def summary(top: int = 3, floor_ms: float = 50.0) -> list[dict[str, Any]] | None:
    """Top `top` legs at or above `floor_ms`, descending; None when inactive or none qualify."""
    acc = _collector.get()
    if not acc:
        return None
    rows = [
        {"name": name, "ms": round(secs * 1000.0, 1)}
        for name, secs in acc.items()
        if secs * 1000.0 >= floor_ms
    ]
    if not rows:
        return None
    rows.sort(key=lambda r: r["ms"], reverse=True)
    return rows[:top]
