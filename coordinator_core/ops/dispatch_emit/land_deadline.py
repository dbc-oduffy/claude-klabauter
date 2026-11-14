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

Charging basis: a non-reserved leg is refused on wall clock past the budget deadline or an outer
clamp. A reserved leg (integrity, commit) is charged by the scope's thread-CPU time against
`LAND_BUDGET_SECS + RESERVE_SECS`, so wall-clock wait never kills a landing whose work is in budget.
Three wall bounds still refuse a reserved leg: the server's result window, and
`WALL_HANG_CEILING_SECS` since scope entry (a true hang). The dispatcher's remaining time and the
budget's wall deadline do not: an abandoned caller's outcome is still recorded in land-legs.json.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from coordinator_core.telemetry import spawn_counter

#: Seconds one land_run may spend before its next leg halts; well under the 30s op budget.
LAND_BUDGET_SECS = 10.0

#: Seconds past `LAND_BUDGET_SECS` that only the `RESERVED_LEGS`, and every leg checked once one has
#: started in the scope, may start in, so a reviewed landing is not refused by a deadline its optional
#: legs spent. Outer clamps (dispatcher, server window) bound both deadlines and stay authoritative.
RESERVE_SECS = 3.0

#: Final name segments of the legs that draw on the reserve (`land_run.integrity`, `*.commit`).
RESERVED_LEGS = frozenset({"integrity", "_integrity", "commit"})

#: Wall seconds since scope entry after which a reserved leg is refused whatever its process time.
#: Hang bound only, never a work budget: work is charged in thread-CPU seconds against the budget.
WALL_HANG_CEILING_SECS = 25.0

REFUSED = "land-deadline"

#: Pseudo-leg naming the span from scope entry to the first `check`.
SCOPE_LEG = "land_deadline.scope_entry"

#: Monotonic clock seam; tests substitute a fake clock so nothing sleeps.
_now = time.monotonic

#: Per-thread CPU clock seam for a leg's `process_time_ms`; children's CPU is invisible to it, which
#: is why `spawns` is recorded beside it. Tests substitute a fake.
_cpu = time.thread_time

_DEADLINE: ContextVar[Optional[float]] = ContextVar("land_run_deadline", default=None)

#: Seconds the request waited for a pool worker before the handler ran; declared by `set_queue_wait`.
_QUEUE_WAIT: ContextVar[float] = ContextVar("land_run_queue_wait", default=0.0)

#: Deadline for the reserved legs (>= `_DEADLINE`); None when unbound.
_HARD: ContextVar[Optional[float]] = ContextVar("land_run_hard_deadline", default=None)

#: Per-scope ledger of started legs as `(leg, wall_start, cpu_start, spawns_at_start, bytes_at_start)`; None outside
#: a scope.
_LEGS: ContextVar[Optional[list]] = ContextVar("land_run_legs", default=None)

#: True once a reserved leg has been recorded in the innermost scope; keeps `check` O(1) per leg.
#: Trap: a ContextVar set inside a `copy_context().run` worker never reaches the parent, unlike the
#: shared `_LEGS` list; a reserved leg must be checked in the scope's own context.
_RESERVED_OPEN: ContextVar[bool] = ContextVar("land_run_reserved_open", default=False)

#: The server's result window (seconds from submit) the request ran under; None when unbound.
_SERVER_WINDOW: ContextVar[Optional[float]] = ContextVar("land_run_server_window", default=None)


#: Which bound set a deadline: the leg budget itself, the dispatcher's remaining time, or the
#: server's result window.
BOUND_BUDGET = "budget"
BOUND_DISPATCHER = "dispatcher"
BOUND_SERVER_WINDOW = "server-window"
BOUND_WALL_CEILING = "wall-ceiling"

#: Wall deadline of the server-window clamp (earliest across nested scopes); None when unbound.
#: Authoritative for reserved legs, unlike the dispatcher clamp.
_SERVER_CLAMP: ContextVar[Optional[float]] = ContextVar("land_run_server_clamp", default=None)

#: Wall deadline at which a reserved leg is refused as a hang; `excepted()` moves it later.
_HANG_AT: ContextVar[Optional[float]] = ContextVar("land_run_hang_at", default=None)

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
    server_clamps = [c for c, source in clamps if source == BOUND_SERVER_WINDOW] + [
        c for c in (_SERVER_CLAMP.get(),) if c is not None
    ]
    server_clamp = min(server_clamps, default=None)
    hang_at = None if deadline is None else _now() + WALL_HANG_CEILING_SECS
    outer_hang = _HANG_AT.get()
    if outer_hang is not None and (hang_at is None or outer_hang < hang_at):
        hang_at = outer_hang
    server_token = _SERVER_CLAMP.set(server_clamp)
    hang_token = _HANG_AT.set(hang_at)
    token = _DEADLINE.set(deadline)
    hard_token = _HARD.set(hard)
    bound_token = _BOUND_BY.set((deadline_by, hard_by))
    ceiling_token = _CEILING.set(ceiling)
    # Seeded with the scope's own entry: without it, time before the first `check` (imports, token
    # redemption) is in no leg, and a first-leg halt reports `legs: []` (75f803).
    spawn_token = spawn_counter.bind_scope()
    bytes_token = _bind_byte_counter()
    legs_token = _LEGS.set([_ledger_entry(SCOPE_LEG, _now())])
    open_token = _RESERVED_OPEN.set(False)
    try:
        yield
    finally:
        _RESERVED_OPEN.reset(open_token)
        _LEGS.reset(legs_token)
        _reset_byte_counter(bytes_token)
        spawn_counter.reset_scope(spawn_token)
        _CEILING.reset(ceiling_token)
        _BOUND_BY.reset(bound_token)
        _HARD.reset(hard_token)
        _DEADLINE.reset(token)
        _HANG_AT.reset(hang_token)
        _SERVER_CLAMP.reset(server_token)


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
        hang_at = _HANG_AT.get()
        if hang_at is not None:
            _HANG_AT.set(hang_at + spent)


def expired() -> bool:
    """True once the bound budget deadline has passed; False when none is bound."""
    deadline = _DEADLINE.get()
    return deadline is not None and _now() >= deadline


def check(leg: str) -> None:
    """Raise `LandDeadlineExceeded(leg)` when the deadline has passed, carrying the started legs'
    elapsed; otherwise record the leg's start. A no-op when no deadline is bound."""
    now = _now()
    reserved = _is_reserved(leg) or _reserved_leg_open()
    if reserved:
        bound = _reserved_refusal(now)
    else:
        by = _BOUND_BY.get() or (BOUND_BUDGET, BOUND_BUDGET)
        bound = by[0] if expired() else None
    if bound is not None:
        raise LandDeadlineExceeded(leg, _legs_elapsed(now), bound)
    _record(leg, now)


def _reserved_refusal(now: float) -> Optional[str]:
    """The bound that refuses a reserved leg, or None. Charged by scope thread-CPU against
    budget plus reserve; wall time refuses only past the server window or the hang ceiling."""
    if _HARD.get() is None:
        return None
    server = _SERVER_CLAMP.get()
    if server is not None and now >= server:
        return BOUND_SERVER_WINDOW
    hang_at = _HANG_AT.get()
    if hang_at is not None and now >= hang_at:
        return BOUND_WALL_CEILING
    ledger = _LEGS.get()
    if ledger and LAND_BUDGET_SECS is not None and _cpu() - ledger[0][2] >= LAND_BUDGET_SECS + RESERVE_SECS:
        return BOUND_BUDGET
    return None


def _record(leg: str, now: float) -> None:
    """Append `leg` to the scope ledger and latch the reserved-open flag (O(1) per leg)."""
    ledger = _LEGS.get()
    if ledger is not None:
        ledger.append(_ledger_entry(leg, now))
        if _is_reserved(leg):
            _RESERVED_OPEN.set(True)


def mark(leg: str) -> None:
    """Record `leg` as started without ever refusing: names a span of the ledger so a halt's
    `leg_costs` attributes the time before the next `check` to it. A no-op outside a scope."""
    _record(leg, _now())


def _bind_byte_counter():
    from coordinator_core.git import commit

    return commit.bind_byte_counter()


def _reset_byte_counter(token) -> None:
    from coordinator_core.git import commit

    commit.reset_byte_counter(token)


def _bytes_read() -> int:
    from coordinator_core.git import commit

    return commit.bytes_read_total()


def _ledger_entry(leg: str, now: float) -> tuple:
    return (leg, now, _cpu(), spawn_counter.scope_spawns(), _bytes_read())


def _is_reserved(leg: str) -> bool:
    """True for the integrity and commit legs and the sub-legs under them, which may spend the
    reserved tail of the budget."""
    return any(seg in RESERVED_LEGS for seg in leg.split("."))


def _reserved_leg_open() -> bool:
    """True once a reserved leg has started in this scope: the integrity leg stays open until the
    commit, so every leg checked after it draws on the reserve."""
    return bool(_RESERVED_OPEN.get())


def _legs_elapsed(now: float) -> list:
    """Each started leg's cost: its start to the next leg's start, the last to `now`. Entries carry
    `leg`, wall `elapsed_secs`, thread-CPU `process_time_ms`, `spawns` made in the scope and
    worktree `bytes_read` by `checkin_blob_sha`."""
    ledger = _LEGS.get() or []
    ends = [entry[1:] for entry in ledger[1:]] + [(now, _cpu(), spawn_counter.scope_spawns(), _bytes_read())]
    return [
        {
            "leg": name,
            "elapsed_secs": max(0.0, end_wall - wall),
            "process_time_ms": round(max(0.0, end_cpu - cpu) * 1000.0, 1),
            "spawns": max(0, end_spawns - spawns),
            "bytes_read": max(0, end_bytes - nbytes),
        }
        for (name, wall, cpu, spawns, nbytes), (end_wall, end_cpu, end_spawns, end_bytes) in zip(ledger, ends)
    ]


def leg_costs() -> list:
    """The current scope's started legs as `[{leg, elapsed_secs, process_time_ms, spawns, bytes_read}]`, the
    last leg costed up to now; empty outside a scope."""
    return _legs_elapsed(_now())


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
        "legs": [{"leg": e["leg"], "elapsed_secs": e["elapsed_secs"]} for e in exc.legs],
        "leg_costs": list(exc.legs),
    }
