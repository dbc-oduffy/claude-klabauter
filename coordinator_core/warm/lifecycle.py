"""coordinator_core.warm.lifecycle — drain-and-exit epilogue, trigger-agnostic.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C17

ONE ordered sequence, reachable from every stop trigger (skew eviction,
idle demotion, operator request, degraded-health self-stop) with NO
trigger-specific branching in the sequence itself:

    1. close listener   -- no new work enters
    2. wait for in-flight to reach zero, bounded by a ceiling
    3. explicit ctx shutdown -- flush the log, unlink the breadcrumb,
       close pipe handles
    4. os._exit(0)

Step 3 MUST precede step 4: `os._exit` skips `atexit` entirely, so any
cleanup not already run by the time `_run_tail` reaches step 4 never runs.
Step 4 MUST be `os._exit`, never `sys.exit` and never a bare return from
`asyncio.run()` -- see the causal-chain note below for why. Both are
pinned by `test_drain_exit_order.py`'s AST guard.

CAUSAL CHAIN (docs-check 2026-08-15 against live CPython 3.13.1 source,
do not re-derive): the mechanism that can hang without bound is
`concurrent/futures/thread.py::_python_exit`, registered via
`threading._register_atexit`, which does a bare `t.join()` on abandoned
`to_thread` workers with NO timeout. It fires at interpreter-level
shutdown (normal process exit / `sys.exit`), not inside `asyncio.run()`.
It is NOT `loop.shutdown_default_executor()` reached via `asyncio.run()`:
on CPython >= 3.12, `asyncio/runners.py`'s `Runner.close()` passes
`constants.THREAD_JOIN_TIMEOUT` (=300) into `shutdown_default_executor()`,
so that path is capped at 300s and cannot itself produce a 9-hour hang.
UNRESOLVED, not asserted here or anywhere else: whether the original ~9h
measurement predates the 300s cap (pre-3.12 interpreter) or traversed the
unbounded `_python_exit` atexit path instead of `asyncio.run()`'s
shutdown -- the evidence does not distinguish these, and this module does
not guess. The re-measured 20.1s (`sys.exit`) vs 1.22s (`os._exit`)
figures stand regardless: `sys.exit` still reaches an unbounded atexit
join in the affected paths, while explicit `ctx.shutdown()` before
`os._exit(0)` is correct against BOTH mechanisms -- `os._exit` skips
`atexit` entirely, sidestepping `_python_exit`'s unbounded join as well
as `shutdown_default_executor`'s bounded one.

SINGLE-SHOT (staff-eng finding 13): the sequence must be entered at most
once per server life, even when two triggers fire near-simultaneously
(e.g. an idle deadline expiring while a skew drain is already mid-flight).
`_try_enter_once()` is an atomic test-and-set behind a `threading.Lock`;
every trigger attempts it, the winner runs the tail, every loser returns
`False` immediately without touching in-flight wait, ctx shutdown, or
`os._exit`. This is the only guard against a demotion trigger observing
a concurrent skew eviction as a second interleaved shutdown.

TWO ENTRY POINTS, one shared tail (`_run_tail`):

  - `begin_shutdown` -- the full 4-step sequence. This is what a trigger
    with no upstream listener-close binds to (idle demotion / C24,
    operator request, degraded self-stop).
  - `drain_and_exit` -- steps 2-4 only. This is what `warm.skew.
    evict_on_skew`'s `drain` argument binds to: `evict_on_skew` already
    runs `respond` then `close_listener` itself (step 1) before calling
    `drain()`, so binding the full `begin_shutdown` there would run
    `close_listener` twice. Both entry points share the same single-shot
    guard, so whichever trigger's `_try_enter_once()` wins is the only
    one that ever reaches the in-flight wait, `ctx_shutdown()`, or
    `os._exit`.

Every side effect is caller-injected (`close_listener`, `in_flight_count`,
`ctx_shutdown`, `exit_fn`) rather than reached through a concrete server
context class -- no such class exists yet at C17. This mirrors C16's
`evict_on_skew` shape (this module's own docstring calls out the parallel)
and is deliberate: C24's idle-demotion entry point, and any later trigger,
bind to `begin_shutdown` by supplying their own callables rather than this
module importing a context type that would create a layering cycle.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

__all__ = [
    "DRAIN_CEILING_MARGIN_SECS",
    "begin_shutdown",
    "drain_and_exit",
    "reset_shutdown_guard_for_test",
    "set_final_sweep_hook",
    "reset_final_sweep_hook_for_test",
]

# Retired SINGLETON_BLOCKING_ACQUIRE_TIMEOUT_SECS used DISPATCH_TIMEOUT_SECS
# + 5s margin; this keeps only the margin half live, since the ceiling now
# tracks `ipc`'s dispatch timeout dynamically (see `_drain_ceiling_secs`)
# rather than pinning a duplicate constant that could drift from it.
DRAIN_CEILING_MARGIN_SECS = 5.0

_DRAIN_POLL_INTERVAL_SECS = 0.05

_guard_lock = threading.Lock()
_shutdown_entered = False

#: The mandatory final-sweep hook (`warm.push_cadence`'s C4), run inside
#: `_run_tail` above `exit_fn` -- see that function's own docstring. A
#: zero-arg callable with no return value, or `None` (the default) when no
#: caller has registered one. Registered as a settable module-level hook
#: rather than threaded through `begin_shutdown`/`drain_and_exit`'s own
#: kwargs because FOUR independent call sites reach these two entry points
#: -- `warm/front_door.py:1218`, `warm/idle.py:285`, `warm/supervisor.py:898`
#: (all `begin_shutdown`), and `warm/server.py:1631` (`drain_and_exit`) --
#: and only `server.py` holds a reference to the sweep it must run. A kwarg
#: would have to be threaded through all four, and a caller that forgot it
#: would produce a silent no-sweep exit; a module-global hook makes "every
#: exit sweeps" structural, reachable from every trigger without each call
#: site needing to know the sweep exists. (Re-verified 2026-08-30 against
#: `state/audits/2026-08-30-four-push-close-backlog-items-probe.py` leg G;
#: Finding 8's premise -- that this can now collapse into a threaded kwarg
#: because C4's idle.py write-scope constraint expired -- is FALSE: the
#: constraint that mattered was never idle.py's writable-scope status, it
#: was these four callers' inability to source the sweep themselves.)
_final_sweep_hook_lock = threading.Lock()
_final_sweep_hook: Optional[Callable[[], None]] = None


def set_final_sweep_hook(hook: Optional[Callable[[], None]]) -> None:
    """Register (or clear, with `None`) the final-sweep hook every shutdown
    sequence runs, once, above `exit_fn` -- see the module-level docstring
    on `_final_sweep_hook` for why this is a settable hook rather than a
    `begin_shutdown`/`drain_and_exit` kwarg. `warm.server._ServerContext`
    binds this once at boot to its own live served-repo sweep; nothing else
    in this module ever calls it.
    """
    global _final_sweep_hook
    with _final_sweep_hook_lock:
        _final_sweep_hook = hook


def reset_final_sweep_hook_for_test() -> None:
    """Test-only: clear the registered hook so a fresh test starts from
    "nothing registered". Never called by production code.
    """
    set_final_sweep_hook(None)


def _try_enter_once() -> bool:
    """Atomic test-and-set: True for exactly one caller across this
    process's life, False for every other (concurrent or later) caller.
    """
    global _shutdown_entered
    with _guard_lock:
        if _shutdown_entered:
            return False
        _shutdown_entered = True
        return True


def reset_shutdown_guard_for_test() -> None:
    """Test-only: clear the single-shot guard so a fresh test can exercise
    entry again. Never called by production code -- a real server process
    shuts down at most once by construction, so there is no live call site
    for this outside `test_drain_exit_order.py`.
    """
    global _shutdown_entered
    with _guard_lock:
        _shutdown_entered = False


def _drain_ceiling_secs() -> float:
    """35s by default: `ipc`'s live-reread dispatch timeout (30s default,
    re-tunable via `COORDINATOR_DISPATCH_TIMEOUT_SECS` with no restart)
    plus `DRAIN_CEILING_MARGIN_SECS`. Imported locally to avoid a module
    -level import cycle between `warm` and `ipc`.
    """
    from coordinator_core.ipc import _resolve_dispatch_timeout_secs

    return _resolve_dispatch_timeout_secs() + DRAIN_CEILING_MARGIN_SECS


def _wait_for_drain(
    in_flight_count: Callable[[], int],
    *,
    ceiling_secs: float,
    poll_interval: float = _DRAIN_POLL_INTERVAL_SECS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll `in_flight_count()` until it reaches zero or `ceiling_secs`
    elapses. Returns True on a clean drain, False if the ceiling was hit.

    The ceiling bounds shutdown LATENCY -- it is not a guarantee that no
    in-flight request was interrupted; a caller that hits it still
    proceeds to `ctx_shutdown()` and `os._exit(0)` (see `_run_tail`),
    since an unbounded wait would reintroduce exactly the kind of
    unbounded shutdown hang this chunk exists to close off.
    """
    deadline = clock() + ceiling_secs
    while in_flight_count() > 0:
        if clock() >= deadline:
            return False
        sleep(poll_interval)
    return True


def _run_tail(
    *,
    in_flight_count: Callable[[], int],
    ctx_shutdown: Callable[[], None],
    exit_fn: Callable[[int], None],
    drain_ceiling_secs: Optional[float],
) -> None:
    """Steps 2-4, shared by both entry points below. AST-pinned by
    `test_drain_exit_order.py`: must call `exit_fn` (defaulting to
    `os._exit`) as its last action and must never call `sys.exit`.

    A registered final-sweep hook (`set_final_sweep_hook`) runs after
    `ctx_shutdown()` and before `exit_fn`, on EVERY path through this
    function regardless of which entry point (`begin_shutdown` or
    `drain_and_exit`) reached it -- this is what makes the cadence's
    "always sweep before the box goes quiet" bound non-vacuous for idle
    demotion and superseded-generation retirement, not merely for skew
    eviction. Swallows any exception the hook raises: a sweep failure must
    never prevent `exit_fn` from running.
    """
    ceiling = _drain_ceiling_secs() if drain_ceiling_secs is None else drain_ceiling_secs
    _wait_for_drain(in_flight_count, ceiling_secs=ceiling)
    ctx_shutdown()
    hook = _final_sweep_hook
    if hook is not None:
        try:
            hook()
        except Exception:  # noqa: BLE001 -- a sweep failure must not block exit
            pass
    exit_fn(0)


def begin_shutdown(
    *,
    close_listener: Callable[[], None],
    in_flight_count: Callable[[], int],
    ctx_shutdown: Callable[[], None],
    exit_fn: Callable[[int], None] = os._exit,
    drain_ceiling_secs: Optional[float] = None,
) -> bool:
    """Full 4-step shutdown sequence: close listener -> wait for drain ->
    ctx shutdown -> exit. The entry point for any trigger that has not
    already closed the listener itself -- idle demotion (C24), operator
    request, degraded self-stop.

    STEP 1 STOPS ACCEPTING; IT DOES NOT RELEASE THE ENDPOINT. The
    `close_listener` production binds here (`warm/server.py ::
    _ServerContext.close_listener`, read its docstring) flips a flag; the
    OS-level close and unlink are step 3's, behind the drain. So a
    same-token successor -- which is what idle demotion always produces --
    cannot bind until this whole sequence completes.

    Single-shot across the whole process: returns False immediately,
    touching none of the four steps, if another trigger already won entry
    (concurrently or earlier). Returns True if this call ran the sequence
    -- in production that return is unreachable past `os._exit(0)`, but it
    lets a test with a faked `exit_fn` observe which caller won.
    """
    if not _try_enter_once():
        return False
    close_listener()
    _run_tail(
        in_flight_count=in_flight_count,
        ctx_shutdown=ctx_shutdown,
        exit_fn=exit_fn,
        drain_ceiling_secs=drain_ceiling_secs,
    )
    return True


def drain_and_exit(
    *,
    in_flight_count: Callable[[], int],
    ctx_shutdown: Callable[[], None],
    exit_fn: Callable[[int], None] = os._exit,
    drain_ceiling_secs: Optional[float] = None,
) -> bool:
    """Steps 2-4 only: wait for drain -> ctx shutdown -> exit, sharing
    `begin_shutdown`'s single-shot guard. Bind this as `warm.skew.
    evict_on_skew`'s `drain=` argument -- `evict_on_skew` already runs
    `close_listener` itself (step 1) before invoking `drain()`, so this
    entry point does not repeat it.
    """
    if not _try_enter_once():
        return False
    _run_tail(
        in_flight_count=in_flight_count,
        ctx_shutdown=ctx_shutdown,
        exit_fn=exit_fn,
        drain_ceiling_secs=drain_ceiling_secs,
    )
    return True
