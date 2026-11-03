"""review.prep per-leg CPU attribution: leg-name registry, `leg()` and `run()`.

`run(fn)` collects the handler thread's per-leg `thread_time` (via `leg_attribution`) plus
each leg's charged child CPU, and attaches the top legs to the dispatched row as `legs`.

Trap: `thread_time` misses child processes. `git.run.run_git` appends each git child's Job CPU
to `ipc._op_cpu_secs_var` (Windows only), so a leg's child cost is the growth of that list
across its body. The handler's own delta is appended only after the handler returns, so
in-body growth is child CPU only.

Trap: legs are inclusive, not exclusive. `peer_bases` runs inside `stored_reply` (served() calls
it lazily), so its cost appears under both names; read them as nested, not additive.
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, TypeVar

from coordinator_core.bash_guards import leg_attribution
from coordinator_core.telemetry import op_row_fields

T = TypeVar("T")

LEGS: tuple[str, ...] = (
    "handler.declared_paths",
    "handler.undeclared_edits",
    "handler.halted_withheld",
    "parse_gate",
    "peer_bases",
    "stored_reply",
    "freeze_whole",
    "hash_contents",
    "range_blob_bytes",
    "pre_review_snapshot",
    "partition",
    "foreign_claims",
    "write_sidecar",
)

TOP = 3
FLOOR_MS = 50.0

_child_secs: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "review_prep_legs_child_secs", default=None
)


class _NoopLeg:
    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


_NOOP = _NoopLeg()


def _charged_secs() -> float:
    from coordinator_core import ipc

    bucket = ipc._op_cpu_secs_var.get()
    return sum(bucket) if bucket else 0.0


class _Leg:
    __slots__ = ("_acc", "_inner", "_name", "_c0")

    def __init__(self, acc: dict[str, float], name: str) -> None:
        self._acc = acc
        self._name = name
        self._inner = leg_attribution.leg(name)
        self._c0 = 0.0

    def __enter__(self) -> None:
        self._c0 = _charged_secs()
        self._inner.__enter__()
        return None

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(*exc)
        delta = _charged_secs() - self._c0
        self._acc[self._name] = self._acc.get(self._name, 0.0) + delta
        return None


def leg(name: str) -> Any:
    """Context manager charging this body's thread CPU and child CPU to `name`; no-op outside `run`."""
    acc = _child_secs.get()
    if acc is None:
        return _NOOP
    return _Leg(acc, name)


def _summary(acc: dict[str, float]) -> list[dict[str, Any]] | None:
    own = leg_attribution.summary(top=len(acc) + len(LEGS), floor_ms=0.0) or []
    own_ms = {r["name"]: r["ms"] for r in own}
    rows = []
    for name, child_secs in acc.items():
        ms = own_ms.get(name, 0.0)
        child_ms = round(child_secs * 1000.0, 1)
        if ms + child_ms >= FLOOR_MS:
            rows.append({"name": name, "ms": ms, "child_ms": child_ms})
    if not rows:
        return None
    rows.sort(key=lambda r: r["ms"] + r["child_ms"], reverse=True)
    return rows[:TOP]


def run(fn: Callable[[], T]) -> T:
    """Run `fn` with leg collection active; attach `legs` to the row when any leg is over the floor."""
    lt = leg_attribution.begin()
    ct = _child_secs.set({})
    try:
        return fn()
    finally:
        try:
            legs = _summary(_child_secs.get() or {})
            if legs is not None:
                op_row_fields.attach(legs=legs)
        finally:
            _child_secs.reset(ct)
            leg_attribution.end(lt)
