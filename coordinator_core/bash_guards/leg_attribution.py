"""Per-call leg attribution: which legs of one hook call cost time over a floor.

`begin()` activates a collector in the calling context; `with leg(name):` adds the
`time.thread_time` delta (cpu) and the `time.perf_counter` delta (wall) of its body to
`name`; `summary()` names the top legs over a floor, ranked by wall. Inactive, `leg()`
returns a shared no-op and allocates nothing.

Wall minus cpu is the stall shape: a leg blocked on disk, a lock or a child reads ~0 cpu.
A row's `ms` is its cpu time, kept for callers that predate wall time.

Trap: the collector lives in a contextvar, so `begin()` and `leg()`/`summary()` must run
in the same thread context (a hop through `asyncio.to_thread` copies the context).
"""

from __future__ import annotations

import contextvars
import time
from typing import Any

# name -> [cpu_secs, wall_secs]
_collector: contextvars.ContextVar[dict[str, list[float]] | None] = contextvars.ContextVar(
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
    __slots__ = ("_acc", "_name", "_c0", "_w0")

    def __init__(self, acc: dict[str, list[float]], name: str) -> None:
        self._acc = acc
        self._name = name
        self._c0 = 0.0
        self._w0 = 0.0

    def __enter__(self) -> None:
        self._c0 = time.thread_time()
        self._w0 = time.perf_counter()
        return None

    def __exit__(self, *exc: object) -> None:
        cpu = time.thread_time() - self._c0
        wall = time.perf_counter() - self._w0
        slot = self._acc.get(self._name)
        if slot is None:
            self._acc[self._name] = [cpu, wall]
        else:
            slot[0] += cpu
            slot[1] += wall
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
    """Context manager accumulating this body's thread CPU and wall time under `name`."""
    acc = _collector.get()
    if acc is None:
        return _NOOP
    return _Leg(acc, name)


def summary(top: int = 3, floor_ms: float = 50.0) -> list[dict[str, Any]] | None:
    """Top `top` legs whose wall time is at or above `floor_ms`, descending by wall;
    rows are `{name, ms, cpu_ms, wall_ms}` with `ms == cpu_ms`. None when inactive or none qualify."""
    acc = _collector.get()
    if not acc:
        return None
    rows = []
    for name, (cpu, wall) in acc.items():
        wall_ms = wall * 1000.0
        if wall_ms >= floor_ms:
            cpu_ms = round(cpu * 1000.0, 1)
            rows.append({"name": name, "ms": cpu_ms, "cpu_ms": cpu_ms, "wall_ms": round(wall_ms, 1)})
    if not rows:
        return None
    rows.sort(key=lambda r: r["wall_ms"], reverse=True)
    return rows[:top]
