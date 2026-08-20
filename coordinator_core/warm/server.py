"""coordinator_core.warm.server — the warm engine's own process entrypoint.

ADDED 2026-08-18 AT EXECUTION, not present in the authored spine of
docs/plans/2026-08-16-one-engine-for-the-whole-box.md — chunk C30. C14 elects
a pipe, C15 connects to one, C16 evicts a stale one, C17 shuts one down, C18
spawns one and writes its breadcrumb, and no chunk built the process those
five describe. This module is that process: the exact script
`coordinator_core.warm.client.SERVER_ENTRY_SCRIPT` names as its spawn target.

WHAT THIS MODULE OWNS, and only this:
  - the process entrypoint C18's spawn targets (`main()`, run via
    `if __name__ == "__main__"`, since `detached_spawn.spawn_detached`
    respawns by resolved interpreter path against this file, never `-m`);
  - the accept loop over the pipe handle `warm.election.elect()` returns,
    including creating and connecting FURTHER (non-first) pipe instances --
    election itself only ever contests the FIRST instance;
  - per-connection framed-NDJSON read, dispatch into the existing engine
    core (`coordinator_core.ipc.dispatch_message`), and framed response
    write;
  - the accept-and-queue boundary between the two: acceptance (bounded by
    `PENDING_LISTENER_POOL_SIZE`) is decoupled from dispatch (bounded by
    `WORKER_POOL_SIZE`) via one `queue.Queue` every accept chain enqueues
    onto and a fixed pool of worker threads drains -- see
    `_ServerContext._enqueue_connection` / `_worker_loop` and DRAIN
    SEMANTICS below for the guarantee this buys (R6/R7);
  - boot identity: `warm.skew.ServerVersionState` constructed ONCE at boot,
    held for the pipe's own generation token and for skew responses.

WHAT THIS MODULE MUST NOT REIMPLEMENT -- every one of these already exists
and a second copy is the failure mode this row is most likely to produce:
  - election -> `warm.election.elect()` / `pipe_name()`. This module only
    creates FOLLOW-ON pipe instances after the first (never re-elects), and
    reuses `election._build_security_attributes` for their ACL rather than
    hand-rolling a second SDDL descriptor -- the one reach into a private
    peer symbol in this module, done because the alternative (duplicating
    the restricted-DACL construction the 2026-08-14 transport spike proved
    out) is the worse of the two costs. Worth promoting to `election.py`'s
    public surface in a future chunk; not done here since `election.py` is
    outside this row's `writes:`.
  - staleness -> `warm.skew`, including the listener-close-before-drain
    order (`evict_on_skew`).
  - shutdown -> `warm.lifecycle`'s single ordered, single-shot sequence;
    this module calls `drain_and_exit` (bound as `evict_on_skew`'s `drain`
    argument -- `evict_on_skew` already closes the listener itself, so
    binding `begin_shutdown` there would double-close it) and never re-rolls
    the close-listener/wait-drain/ctx-shutdown/exit sequence by hand.
  - warm on/off -> `warm.settings` (consulted by the CLIENT before it ever
    spawns this process; this module does not re-check it at boot).
  - breadcrumb -> `warm.breadcrumb`. `main()` writes it once election has
    been WON (never before -- a process that loses the election returns 0
    without touching the winner's breadcrumb), and `_ctx_shutdown` unlinks
    it as step 3 of the lifecycle sequence.
  - idle demotion -> `warm.idle`. This module's own watchdog thread
    (`_ServerContext._idle_watchdog_loop`) is the trigger this row wires
    in: it must fire even when the accept loop never receives a single
    connection, so it cannot be reached only from the request path. The
    same watchdog carries the superseded-generation retirement: this
    module supplies the local predicate (`_token_is_stale`, comparing the
    boot token embedded in its own pipe name against a live
    `skew.compute_client_token`) and `warm.idle` owns the decision. That
    split is why retiring a superseded generation needed no traffic, no
    bind-time handshake, and no change to `evict_on_skew` -- whose
    respond -> close_listener -> drain ordering is untouched.
  - lifecycle telemetry -> `warm.telemetry`. `_ServerContext.telemetry` is
    constructed once at boot, `record_invocation`/`record_exit` are called
    from the request and shutdown-trigger paths below, and `flush()` runs
    from `_ctx_shutdown`.

TRANSPORT MODEL -- synchronous, one OS thread per connection, not asyncio
end to end. `warm.lifecycle`'s shutdown sequence is itself synchronous
(`time.sleep`-based polling in `_wait_for_drain`), and the client's own
transport (`warm.client._open_pipe`) is plain blocking `open(pipe, "r+b")`
-- both match the classic non-overlapped Win32 named-pipe server shape the
2026-08-14 transport spike measured, not the asyncio `PipeServer` /
`start_serving_pipe` machinery that spike also exercised (that route
requires monkeypatching a private `asyncio.windows_events` attribute to get
the restricted ACL onto EVERY instance, which is a heavier, more fragile
surface than driving `_winapi.CreateNamedPipe` / `ConnectNamedPipe`
directly). Each pipe instance is wrapped via `msvcrt.open_osfhandle` +
`os.fdopen(fd, "r+b")` into the SAME blocking file-object API the client
already uses -- verified end-to-end on this box before landing (throwaway
probe, discarded). One thread per connection is what gives "a wedged op
does not stall the next" for free: a hung dispatch wedges only its own
thread's `readline`/dispatch/`write`, never the accept loop or any other
connection's thread -- true today for the bounded `WORKER_POOL_SIZE` pool
of connection-handling threads exactly as it was for the unbounded set of
accept-chain threads that used to double as handlers: a wedge on one
worker stalls only that worker, and the other `WORKER_POOL_SIZE - 1`
workers keep draining the queue.

PER-REQUEST STATE IS NOT OPTIONAL. `_run_dispatch` opens
`coordinator_core.warm.entry_seam.per_request_state()` around every call
into `coordinator_core.ipc.dispatch_message` -- explicit, Token/reset-scoped
per dispatch, per the seam's own contract. `dispatch_message` ->
`_dispatch_message_impl` already carries its own independent declared-writes
Token/reset scope internally (entry_seam's own docstring, "path 1 ...
Converged"), so this wrap nests with it rather than duplicating a dialect;
nesting is safe by `session.declared_writes.collecting()`'s own contract.
Binding explicitly here, rather than relying solely on path 1's internal
convergence, is deliberate: it is what makes this module's own per-request
scoping visible and testable at the boundary this row owns, independent of
`ipc.py`'s internals ever changing. As of C-warm-identity, `per_request_
state` ALSO binds the calling session's identity for the same scope (`_serve_
line` pops `_session_id` off the request and threads it through) -- this
server process's OWN environment (whoever spawned it) must never leak into
`session.core.resolve_session_id()` for a request some OTHER session sent;
see `session.core.session_identity_override`'s docstring for the full
defect this closes.

DISPATCH CONCURRENCY MODEL -- ADDED 2026-08-19, chunk C6 of docs/plans/
2026-08-19-the-fired-path-reaches-the-engine.md, gated on C1's verdict
(docs/research/warm-engine-premise/c1-binding-constraint.md). C1's Arm A
isolated dispatch from transport entirely (`dispatch_message` called
in-process, no pipe, no accept chain, no second process) and reproduced the
serialization curve anyway -- p50 1.25ms -> 29.17ms across 1->32 concurrent
Python THREADS, throughput plateaued ~1000-1100/s from 4 threads up. That is
GIL contention inside dispatch, not a transport artifact, and no amount of
restructuring the per-connection thread/event-loop shape removes it: the GIL
serializes CPU-bound bytecode execution across THREADS in one process
regardless of how the calling code is organized. `_worker_loop` therefore
does not call `_run_dispatch` (in-process) for a REAL accept loop; it calls
`_ServerContext._pool_dispatch`, which submits the request to a
`DISPATCH_PROCESS_POOL_SIZE`-worker `concurrent.futures.ProcessPoolExecutor`
(`_pool_dispatch_worker`, `_worker_process_init`) and blocks the connection's
own thread for the result. The listener/accept/queue layers above this are
UNCHANGED -- this row does not touch transport (anti-scope, C6's own body) --
only what backs the dispatch call a queued connection's worker thread makes.
`_run_dispatch` itself is UNCHANGED and stays the default `dispatch=` this
module's own test suite (and any other in-process caller) exercises directly.

DRAIN SEMANTICS (P5, AC8) -- `in_flight` increments at ENQUEUE, not at
worker pickup: `_ServerContext._enqueue_connection` calls `in_flight.enter()`
before the connection's `io` object is ever put on the queue, so a request
that has been accepted off the wire and is sitting behind a busy worker
already counts as in-flight. `warm.lifecycle.drain_and_exit`'s wait-for-zero
therefore never returns while accepted-but-not-yet-dispatched work remains
queued -- a drain waits for the QUEUE TO EMPTY, never merely for the workers
that happen to be busy right now to finish. This is one step later than
`_handle_connection`'s own increment (`in_flight.enter()` called again,
guarded by `already_entered=True` on the queue path so the slot is not
double-counted, before `io.readline()`) -- enqueue precedes worker pickup,
which precedes the first byte being read. AC7 (bounded dispatch) and AC8
(no queued-but-unstarted work dropped at shutdown) are the same mechanism
observed from two ends: a fixed `WORKER_POOL_SIZE` pool is what makes
acceptance and dispatch independently bounded, and enqueue-time counting is
what makes that boundedness safe to drain.

NEVER FAIL A CALLER. Every fault below the frame-parse boundary --
malformed JSON, a non-object frame, a raised handler exception -- returns a
well-formed JSON-RPC error envelope over the SAME connection rather than
closing it uncleanly or leaving the accept loop's other threads affected;
`_serve_line` never lets an exception escape to its caller. A client always
sees either a well-formed response, a closed pipe, or a dead pipe -- never a
hang, never a malformed frame -- so it can always fall back to cold (C15
Backstop 2, mirrored here on the server's own side of the same contract).

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C30;
gap found and named during C15 execution, 2026-08-18.
"""

from __future__ import annotations

import concurrent.futures
# `concurrent.futures.process` is a LAZY submodule attribute: referencing it as
# `concurrent.futures.process.BrokenProcessPool` inside an `except` clause raises
# AttributeError until something has already built a ProcessPoolExecutor. Imported
# eagerly so the recovery path cannot fail on the way to handling a failure.
from concurrent.futures.process import BrokenProcessPool
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ipc import INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR
from coordinator_core.telemetry import op_latency
from coordinator_core.warm import breadcrumb, election, idle, lifecycle, skew, telemetry
from coordinator_core.warm.engine_root import current_engine_clone
from coordinator_core.warm.entry_seam import per_request_state
# Reached into directly, not duplicated, despite this module's own
# negative-spec preferring a local copy for a one-line peer computation
# (see `_engine_clone_root`'s docstring): `_op_may_mutate`'s fail-closed
# classification and `WARM_DISPATCH_INDETERMINATE`'s error code are
# safety-critical to keep byte-identical between client and server -- a
# drifted copy here could let the two sides disagree about which ops are
# safe to re-run, which is exactly the double-execution hazard this
# import exists to prevent. `client.py` does not import this module (see
# its own module docstring), so this edge is acyclic.
from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE, _op_may_mutate

__all__ = [
    "InFlightCounter",
    "PENDING_LISTENER_POOL_SIZE",
    "WORKER_POOL_SIZE",
    "DISPATCH_PROCESS_POOL_SIZE",
    "main",
]

# How often the idle watchdog re-checks `idle.should_demote` -- independent
# of request arrival (module docstring's "idle demotion" ownership note).
# Small relative to both `idle.ZERO_SERVED_DEADLINE_SECS` (90s) and
# `idle.DEFAULT_IDLE_MINUTES` (15min) so a stranded, zero-invocation server
# demotes close to its deadline rather than one extra poll interval late.
_IDLE_WATCHDOG_POLL_SECS = 5.0

# Mirrors `_winapi.ERROR_PIPE_CONNECTED` (535) -- a client already connected
# before this instance's own `ConnectNamedPipe` call was posted, which is a
# WIN for a synchronous server, not a failure (the same "already connected"
# race non-overlapped named-pipe servers always have to tolerate).
_ERROR_PIPE_CONNECTED = 535

_PIPE_BUFFER_BYTES = 65536

# PENDING LISTENER POOL SIZE -- problem 3 of docs/problems/2026-08-19-the-
# warm-engine-serves-one-caller-at-a-t.md: `_accept_and_replenish` posts
# exactly one replacement per accepted connection, so a server that starts
# with a single pending instance stays at exactly one pending instance for
# its whole life, regardless of demand. `PIPE_UNLIMITED_INSTANCES` is
# already passed at both creation sites (this module and `election.py`) --
# the transport permits a pool, the accept chain just never asked for one.
#
# Formula, inputs named (per `docs/wiki/cost-budgets-and-the-kill-
# disposition.md`'s "derive the bound, don't fit a constant to what the
# code got away with"): `docs/wiki/machine-load-norm.md` already carries a
# standing design assumption for exactly this question -- "estimate lock
# hold time as though 30 callers are queued" -- sized against the same
# 50-70-average/24-floor load norm this server runs under. A pending
# listener is a structurally identical bet: how many simultaneous
# contenders should one shared, short-lived resource be built to absorb
# without degrading into false-absence errors. Reusing that number rather
# than deriving a second one keeps the fleet's concurrency assumptions in
# one place instead of two that can drift apart.
#
# Cost per pending instance, so the tradeoff stays legible: one blocked OS
# thread (parked in a synchronous `ConnectNamedPipe` syscall -- see module
# docstring's "TRANSPORT MODEL") plus one named-pipe kernel object with a
# `_PIPE_BUFFER_BYTES` (64 KiB) read+write buffer. 30 of each is a few MB
# of thread-stack reservation and ~2 MB of pipe buffer, paid only while a
# server is resident and reclaimed in full by `os._exit(0)` on shutdown --
# cheap against the alternative this row exists to close: contention being
# misread as "no server" and converted into spawns (problem 2).
PENDING_LISTENER_POOL_SIZE = 30

# WORKER POOL SIZE -- the accept-and-queue chunk's own bound (docs/plans/
# 2026-08-19-the-fired-path-reaches-the-engine.md § C5, R6/R7): the
# committed baseline (`931d50905`) bounds ACCEPTANCE to
# `PENDING_LISTENER_POOL_SIZE` pending listeners, but each accept chain
# still handles its own connection's dispatch inline on the SAME thread
# that just accepted it, while a fresh replacement thread is spawned
# immediately for the next accept -- so the set of threads doing dispatch
# work grows by one per connection under load, unbounded, exactly the
# shape AC7 forbids ("bounded to a named worker count rather than growing
# one handler thread per accepted connection"). This constant is that
# named bound: `_ServerContext._enqueue_connection` puts every accepted
# connection's `io` object on one `queue.Queue`, and exactly
# `WORKER_POOL_SIZE` long-lived worker threads (`_worker_loop`) drain it,
# so dispatch concurrency is capped independently of how fast connections
# are accepted.
#
# Sized against the same `docs/wiki/machine-load-norm.md` standing
# assumption `PENDING_LISTENER_POOL_SIZE` reuses ("estimate lock hold time
# as though 30 callers are queued") -- this is the sibling question for
# the OTHER shared, short-lived resource (a dispatch worker rather than a
# pending listener): how many simultaneous dispatches should this server
# be built to absorb before excess arrivals wait in the queue rather than
# being dropped or spawning an unbounded thread. Reusing the number rather
# than deriving a second one keeps the fleet's concurrency assumptions in
# one place. Per this row's own body (P4): the queue's product is
# guaranteed acceptance and bounded fan-out damage, NOT lower latency --
# p50 still rises at every worker count P4's prototype swept (1/2/4/8), so
# this value is a damage bound, not a throughput tune.
WORKER_POOL_SIZE = 30

# DISPATCH PROCESS POOL SIZE -- C6's own row (docs/plans/2026-08-19-the-fired-
# path-reaches-the-engine.md), gated on C1's verdict
# (docs/research/warm-engine-premise/c1-binding-constraint.md): Arm A proved
# the p50 rise (1.25ms -> 29.17ms across 1->32 threads) reproduces with ZERO
# transport in the loop -- it is GIL contention inside `dispatch_message`
# itself running on N concurrent Python THREADS, not the accept/pipe chain.
# Restructuring the per-request `asyncio.run()` call cannot remove this: the
# GIL serializes CPU-bound bytecode execution regardless of how many event
# loops or threads submit work to it, so bytecode-level dispatch concurrency
# needs OS-level process isolation, not a different threading shape. Reusing
# `WORKER_POOL_SIZE` keeps one bound instead of a second independently-tuned
# constant -- the process pool replaces the SAME dispatch-concurrency budget
# `WORKER_POOL_SIZE` already names (C5's accept-and-queue chunk), it does not
# add a second one on top of it.
DISPATCH_PROCESS_POOL_SIZE = WORKER_POOL_SIZE


class InFlightCounter:
    """Thread-safe in-flight request counter.

    The `in_flight_count` callable `warm.lifecycle.begin_shutdown` /
    `drain_and_exit` poll to learn when a drain has completed -- one
    instance per server process, incremented when a connection's request is
    accepted for processing and decremented as soon as that request's
    response has been written (not when the connection object is closed),
    so a request that itself triggers the drain (a skew-evicting request)
    has already released its own slot before `drain()` starts polling for
    zero -- see `_serve_line`'s `_release` closure, which is the one call
    site that matters for avoiding a self-deadlock here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def enter(self) -> None:
        with self._lock:
            self._count += 1

    def exit(self) -> None:
        with self._lock:
            self._count -= 1

    def __call__(self) -> int:
        with self._lock:
            return self._count


class _FrameError(Exception):
    """Internal signal carrying a pre-built JSON-RPC error envelope for a
    frame that failed to parse -- never raised past `_serve_line`."""

    def __init__(self, response: dict):
        self.response = response
        super().__init__(response.get("error", {}).get("message", "frame error"))


def _engine_clone_root() -> Path:
    """This server process's own resolved engine-clone root -- collapsed
    onto the single shared definition, `engine_root.current_engine_clone()`
    (plan 2026-08-19-an-engine-root-is-a-stamped-build § C3)."""
    return current_engine_clone()


def _encode(response: dict) -> bytes:
    return json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"


def _parse_frame(raw_line: bytes) -> dict:
    """Decode one NDJSON frame into a JSON-RPC request dict, or raise
    `_FrameError` carrying a well-formed PARSE_ERROR / INVALID_REQUEST
    envelope -- the malformed-frame half of "never fail a caller"."""
    try:
        text = raw_line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _FrameError(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": PARSE_ERROR, "message": f"Parse error: frame is not valid UTF-8 ({exc})"},
            }
        ) from exc

    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _FrameError(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": PARSE_ERROR, "message": f"Parse error: {exc}"},
            }
        ) from exc

    if not isinstance(msg, dict):
        raise _FrameError(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": INVALID_REQUEST,
                    "message": f"Invalid Request: frame must be a JSON object, got {type(msg).__name__}",
                },
            }
        )
    return msg


def _run_dispatch(msg: dict, *, session_id: Optional[str] = None) -> dict:
    """Invoke the existing engine core for one already-parsed JSON-RPC
    request -- the SOLE process-level dispatch chokepoint
    (`coordinator_core.ipc.dispatch_message`'s own docstring), never a
    hand-rolled call into `get_op_handler`.

    Opens `entry_seam.per_request_state()` around the call (module
    docstring's "PER-REQUEST STATE IS NOT OPTIONAL"), and runs the async
    dispatch via a fresh `asyncio.run()` scoped to this one call -- each
    connection already lives on its own OS thread (module docstring's
    "TRANSPORT MODEL"), so a per-call event loop needs no cross-thread
    scheduling and leaves no shared loop state for the next request on this
    thread, or any other connection's thread, to inherit.

    `session_id`, when given (the caller's own resolved identity, carried
    over the wire as the request's `_session_id` field and popped by
    `_serve_line` before this call), is bound for the duration of the
    dispatch via `per_request_state`'s own `session_id` parameter -- see
    that seam's docstring for the full identity-attribution defect this
    closes. `None` (no identity carried) is a no-op bind, reproducing
    today's server-resolves-its-own-env behaviour exactly.
    """
    import asyncio

    from coordinator_core.ipc import dispatch_message

    diagnostics: list = []
    with per_request_state(session_id=session_id, diagnostics=diagnostics):
        response = asyncio.run(dispatch_message(msg))

    # The op's diagnostic lines ride the TRANSPORT frame, never `result` — the
    # wire envelope is frozen (contract §2.1) and a setup error is defined to
    # carry no reason field inside it. `_stderr` is a sibling of `result`,
    # popped by `warm.client` before the response reaches any consumer, so
    # nothing downstream of the client can observe a shape a cold spawn lacks.
    # Without this the reason dies on the SERVER's stderr and a warm-served
    # refusal is mute — see `entry_seam`'s DIAGNOSTIC-axis note for the report
    # that found it.
    if diagnostics and isinstance(response, dict):
        response = {**response, "_stderr": "\n".join(diagnostics)}
    return response


def _pool_dispatch_worker(msg: dict, session_id: Optional[str]) -> dict:
    """The `DISPATCH_PROCESS_POOL_SIZE` worker-process target -- identical
    body to `_run_dispatch`, factored out as its own top-level (picklable)
    function because `concurrent.futures.ProcessPoolExecutor.submit` needs
    an importable `__module__`/`__qualname__` target, not a closure or a
    bound method. Runs entirely inside a WORKER PROCESS, so this is the one
    piece of C6's fix that actually gets dispatch off the accept process's
    GIL (see `DISPATCH_PROCESS_POOL_SIZE`'s own comment): the OS, not the
    interpreter lock, schedules concurrent calls to this function across
    `DISPATCH_PROCESS_POOL_SIZE` separate processes.

    `entry_seam.per_request_state`'s contextvars and `coordinator_core.ipc`'s
    own declared-writes scoping are per-PROCESS state, so opening them here
    (inside the worker) is exactly as safe as opening them on the accept
    process's own connection thread was -- each worker process has entirely
    its own contextvar storage, with no cross-process sharing to guard
    against.
    """
    import asyncio

    from coordinator_core.ipc import dispatch_message

    diagnostics: list = []
    with per_request_state(session_id=session_id, diagnostics=diagnostics):
        response = asyncio.run(dispatch_message(msg))

    if diagnostics and isinstance(response, dict):
        response = {**response, "_stderr": "\n".join(diagnostics)}
    return response


_POOL_BROKEN_INDETERMINATE_MESSAGE = (
    "warm dispatch indeterminate: this MUTATING op was submitted to the warm "
    "engine's dispatch process pool, and the worker executing it died "
    "(BrokenProcessPool) before a result came back. The op may have "
    "COMPLETED -- a dead worker is not proof the work was not performed. "
    "Reconcile against real state (e.g. `git log`) before re-running; "
    "re-running blind is how a duplicate commit happens. Deliberately NOT "
    "re-run in-process here: re-executing a delivered mutation whose "
    "outcome is unknown is exactly the double-execution this refusal "
    "prevents."
)


def _pool_broken_indeterminate_envelope(msg: dict) -> dict:
    """A JSON-RPC error envelope for a MUTATING op whose pool worker died
    with the request's outcome unknown -- the server-side sibling of
    `warm.client._indeterminate_envelope`, same `WARM_DISPATCH_INDETERMINATE`
    code so the client's existing pass-through (any well-formed envelope
    that is not ENGINE_SKEW is returned to the caller verbatim) surfaces
    this exactly as it would surface a client-detected indeterminate case.

    Returned, never raised: `_pool_dispatch`'s caller (`_serve_line`) treats
    a raised exception as a generic INTERNAL_ERROR, which would erase the
    distinction this envelope exists to carry (the op may have SUCCEEDED,
    not merely failed).
    """
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {
            "code": WARM_DISPATCH_INDETERMINATE,
            "message": _POOL_BROKEN_INDETERMINATE_MESSAGE,
        },
    }


def _declare_execution_route() -> None:
    """Stamp this process's op-latency rows as the `warm_server` route (AC6c).

    `telemetry.op_latency.execution_route` reads
    `COORDINATOR_EXECUTION_ROUTE` and defaults to `in_process` for every
    caller that has not declared itself. Nothing declared itself until this
    call existed, so every row in the corpus -- including rows written by
    this server, which is by definition NOT the in-process route -- read
    `in_process`, and `op_latency.double_routed_corr_ids` could not fire:
    a detector for one `corr_id` under two routes is inert while the corpus
    holds exactly one route value.

    AN ENV VAR, NOT AN IMPORT, AND THE DIRECTION MATTERS. `op_latency` sits
    on the dispatch hot path and must never import `warm.server` (or
    anything that pulls the engine in) to answer "which route am I" -- so
    the serving process declares itself outward instead. Do not invert this
    by having telemetry ask the server.

    POOL WORKERS INHERIT IT, WHICH IS WHY `_worker_process_init` DOES NOT
    SET IT. Since C6, the process that executes an op -- and therefore the
    process that writes its record -- is a `ProcessPoolExecutor` worker
    (`_pool_dispatch_worker`), not this one. `os.environ` assignment goes
    through `putenv`, so the mutation lands in this process's real
    environment block, and multiprocessing's Windows spawn passes that block
    to each worker at `CreateProcess` time; verified live 2026-08-19, worker
    processes read `warm_server`. The pool is built lazily on first dispatch
    (`_ensure_dispatch_pool`), long after this boot-time call, so the
    ordering that inheritance depends on holds by construction. The
    `BrokenProcessPool` fallback in `_pool_dispatch` executes the op in THIS
    process, which carries the same declaration -- the route follows the
    executor either way.
    """
    os.environ[op_latency.ROUTE_ENV] = op_latency.WARM_SERVER


def _suppress_pool_worker_consoles() -> None:
    """Point multiprocessing's Windows spawn at `pythonw.exe` so pool workers
    open no console window.

    `ProcessPoolExecutor` exposes no `creationflags` seam — the whole
    `win_portability.no_console_creationflags` discipline the rest of this
    repo follows is unreachable from here, because multiprocessing builds its
    own `CreateProcess` call. A resident warm server is normally started
    detached (no console of its own), so every worker Windows spawns for it
    gets a BRAND NEW console allocated: a focus-stealing window, one per
    worker, that survives for the pool's whole life. Observed live
    2026-08-19: four such windows, each showing a worker's op-registry
    preload output.

    `sys.executable` is what multiprocessing spawns, and `pythonw.exe` is the
    same interpreter with the console subsystem flag cleared — so swapping it
    suppresses the window without touching the pipe-based worker protocol,
    which never used stdio. Worker stdout/stderr become `None`; the one thing
    that wrote there (a per-worker op-module import failure) is ALSO recorded
    via `logging` and in `coordinator_core.ops._POISONED_MODULES`, which
    re-raises the real cause on dispatch — so no diagnostic is lost, only its
    unread window.

    `None` streams are NOT self-neutralising, and this docstring previously
    claimed they were. `print(..., file=None)` is a no-op, but an op handler
    reaching for the stream OBJECT — `sys.stderr.write(...)`, `.flush()` —
    raises `AttributeError` on `None`, which the dispatcher collapses to a
    bare `-32603 Internal error: AttributeError` with no traceback (the
    worker has no stderr to print one to). 138 such call sites across 34 op
    modules were live when this was found. `_bind_null_std_streams` restores
    writable sinks in every worker so the swap below stays a console fix and
    not a behaviour change; see its own docstring.

    Negative-spec:
        - Does NOT run off Windows, where there is no console to suppress and
          `pythonw` does not exist.
        - Does NOT fall back to some other interpreter if `pythonw.exe` is
          absent beside `sys.executable`: a popup is a nuisance, spawning a
          DIFFERENT Python than the server's own is a correctness hazard.
    """
    if os.name != "nt":
        return
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.is_file():
        multiprocessing.set_executable(str(candidate))


def _wait_for_parent_exit(parent_pid: int) -> None:
    """Block until the process `parent_pid` is gone. Returns, never raises.

    Windows has no POSIX parent-death signal and no process-group teardown,
    so this waits on a handle to the parent instead: `OpenProcess(SYNCHRONIZE)`
    plus an infinite `WaitForSingleObject`, which costs one blocked thread and
    zero polling. A handle that cannot be opened means the parent is already
    gone (or unreachable, which we treat the same way) and returns at once.
    Off Windows, `os.getppid()` re-parenting is the equivalent signal and is
    polled, since there is no handle to wait on.
    """
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        INFINITE = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declared, not left to ctypes' default int marshalling: a HANDLE is
        # pointer-width, and only Windows' documented "handle values are
        # 32-bit-significant" guarantee makes the undeclared form work by
        # accident. Saying so costs three lines and removes the accident.
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if not handle:
            return
        try:
            kernel32.WaitForSingleObject(handle, INFINITE)
        finally:
            kernel32.CloseHandle(handle)
        return
    while os.getppid() == parent_pid:
        time.sleep(1.0)


def _exit_with_parent(parent_pid: int) -> None:
    """Daemon-thread body: outlive nothing. When the server dies, so does this
    worker.

    THE LEAK THIS CLOSES. `_ctx_shutdown` tears the pool down properly, but it
    runs only on the four sanctioned exit triggers that reach `begin_shutdown`
    / `drain_and_exit`. A server that dies any OTHER way -- crash, `taskkill`,
    OOM, a killed console -- never sends the pool its sentinel, and a worker
    blocked on its call-queue `get()` waits for a parent that will never speak
    again. Observed live 2026-08-19: two orphaned workers (PIDs 70016, 11844)
    whose parents had no `Win32_Process` row at all, each still holding the
    console window Windows had allocated it, unreachable except by `taskkill`.

    WHY NOT A JOB OBJECT, which is the textbook Windows answer: a job with
    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` on the SERVER would take down every
    descendant, and `ops/app_session.py`'s `launch` op deliberately spawns
    processes meant to outlive the op that started them -- it persists their
    handles precisely so a later `teardown` can signal them. Killing a
    developer's running app because the engine idled out is a far worse bug
    than the leak being fixed. This watchdog binds ONLY the dispatch pool's
    own workers to the server's lifetime, which is the actual invariant.

    `os._exit`, not `sys.exit`: this runs on a non-main thread, where
    `SystemExit` would be swallowed, and a worker whose parent is gone has
    nothing left to flush.
    """
    _wait_for_parent_exit(parent_pid)
    os._exit(1)


def _bind_null_std_streams() -> None:
    """Give this process writable `sys.stdout`/`sys.stderr` when they are `None`.

    Purpose: a `pythonw.exe`-spawned process (see
    `_suppress_pool_worker_consoles`) has no console and CPython sets both
    standard streams to `None`. Op handlers are ordinary library code written
    against a real interpreter: 138 call sites across 34 modules in
    `coordinator_core/ops/` reach for the stream OBJECT
    (`sys.stderr.write(...)`, `.flush()`), which raises `AttributeError` on
    `None`. Warm-served, that surfaces to the caller as a bare
    `-32603 Internal error: AttributeError` with an empty stderr, and cold
    dispatch of the same op succeeds — a divergence between the warm and cold
    paths, which the warm engine is not allowed to introduce.

    Reported by example-cockpit-repo-em 2026-08-20 against
    `query-records --type plan`, whose `_apply_plan_filename_filter` warning
    is one such site; `plan` was simply the first type on their list that
    writes a diagnostic at all.

    Rebinds to `os.devnull` rather than a buffer: these diagnostics have no
    reader in a detached worker, and an in-memory sink would grow unbounded
    for the life of a resident pool.

    Negative-spec:
        - Does NOT touch a stream that is already bound. A worker that DOES
          have real stdio (any non-`pythonw` spawn, and every cold-dispatch
          process) keeps it, so no diagnostic that reaches an operator today
          is redirected to devnull by this.
        - Does NOT close or replace `sys.__stdout__`/`sys.__stderr__`.
        - Is NOT a substitute for an op emitting an operator-facing
          diagnostic through `logging` or its own result payload. It removes a
          crash, not the reason those 138 sites are the wrong seam.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8", newline="\n"))


def _worker_process_init() -> None:
    """`ProcessPoolExecutor`'s `initializer=` -- runs once per worker
    process, before that process ever dispatches a request, so the
    703ms-first-dispatch op-registry import (`_preload_op_registry`'s own
    docstring) is paid by a process nobody is waiting for rather than by
    whichever request happens to land on a freshly-started worker first.
    Mirrors the main process's own boot-time preload (`main()` step, before
    `_ServerContext.serve_forever`) for the SAME reason, on the process that
    now actually does the dispatching.
    """
    _bind_null_std_streams()
    threading.Thread(
        target=_exit_with_parent,
        args=(os.getppid(),),
        name="warm-pool-parent-watchdog",
        daemon=True,
    ).start()
    _preload_op_registry()


def _serve_line(
    raw_line: bytes,
    *,
    write: Callable[[bytes], None],
    version_state: "skew.ServerVersionState",
    server_sha: Optional[str],
    close_listener: Callable[[], None],
    drain: Callable[[], None],
    release_in_flight: Callable[[], None],
    dispatch: Callable[..., dict] = _run_dispatch,
    mark_invocation: Callable[[], None] = idle.mark_invocation,
    record_invocation: Callable[[bool], None] = lambda warm: None,
    record_exit: Callable[[str], None] = lambda reason: None,
) -> None:
    """Process one request frame for one connection: write exactly one
    response frame (or delegate that write to `warm.skew.evict_on_skew`'s
    own `respond` callable on a detected skew), then release this request's
    in-flight slot. Never raises -- see module docstring's "NEVER FAIL A
    CALLER".

    `release_in_flight` is idempotency-guarded here (not by the caller) so
    it is safe to call it once inline (right after the response is written,
    which is what lets a skew-triggered `drain()` observe this request as
    already-released rather than deadlocking on its own count) and have the
    connection thread's own cleanup call it again as a no-op safety net.

    Pops `_session_id` (the caller's own resolved identity, set by
    `warm.client._try_warm_dispatch_inner`) off `msg` the same way
    `_engine_token` is already popped, and passes it through to `dispatch`
    as a `session_id=` kwarg -- `_run_dispatch`'s own docstring covers the
    bind/no-op contract from there. Absent (older client, or a caller
    `resolve_session_id()` could not identify) is `None`, which is a no-op
    bind, not a fabricated identity.

    `mark_invocation` runs for EVERY frame this function is handed,
    including a skew-evicting one -- `warm.idle`'s own module docstring
    ("IDLE CLOCK OWNERSHIP") calls this out as deliberate, not a bug to
    suppress. `record_invocation` records a served (warm) request for
    `warm.telemetry`; `record_exit` records why this server is about to
    exit, called before `drain` on the skew path so `_ctx_shutdown`'s later
    `telemetry.flush()` observes the reason.
    """
    mark_invocation()
    released = False

    def _release() -> None:
        nonlocal released
        if not released:
            released = True
            release_in_flight()

    def _write_and_release(response: dict) -> None:
        write(_encode(response))
        _release()

    try:
        msg = _parse_frame(raw_line)
    except _FrameError as exc:
        _write_and_release(exc.response)
        return

    request_id = msg.get("id")
    client_token = msg.pop("_engine_token", None)
    caller_session_id = msg.pop("_session_id", None)

    if client_token is not None and version_state.is_skewed(client_token):
        record_exit(telemetry.EXIT_REASON_SKEW)
        skew.evict_on_skew(
            respond=_write_and_release,
            close_listener=close_listener,
            drain=drain,
            request_id=request_id,
            server_sha=server_sha,
            client_token=client_token,
        )
        return

    try:
        response = dispatch(msg, session_id=caller_session_id)
    except Exception as exc:  # noqa: BLE001 -- never fail the caller, see module docstring
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": INTERNAL_ERROR, "message": f"internal error: {exc!r}"},
        }
    record_invocation(True)
    _write_and_release(response)


def _handle_connection(
    io: Any,
    *,
    version_state: "skew.ServerVersionState",
    server_sha: Optional[str],
    close_listener: Callable[[], None],
    drain: Callable[[], None],
    in_flight: InFlightCounter,
    dispatch: Callable[..., dict] = _run_dispatch,
    mark_invocation: Callable[[], None] = idle.mark_invocation,
    record_invocation: Callable[[bool], None] = lambda warm: None,
    record_exit: Callable[[str], None] = lambda reason: None,
    already_entered: bool = False,
) -> None:
    """One connection's request/response lifecycle, run on its own thread.

    Exactly one request per connection, matching `warm.client.
    _try_warm_dispatch_inner`'s own shape (one write, one read, close) --
    there is no live client that keeps a connection open past its single
    request, so this does not loop reading further lines.

    `already_entered`, when True, skips this function's own `in_flight.
    enter()` call because the caller already claimed this request's slot at
    a point that must count as in-flight earlier than this function runs --
    the bounded worker pool's enqueue point (`_ServerContext.
    _enqueue_connection`), per the module docstring's DRAIN SEMANTICS.
    Default False preserves this function's original one-call-does-both
    contract for every direct caller (tests, and any future single-shot
    caller that has not itself already entered).
    """
    if not already_entered:
        in_flight.enter()
    exited = False

    def _exit_once() -> None:
        nonlocal exited
        if not exited:
            exited = True
            in_flight.exit()

    def _write_flushed(data: bytes) -> None:
        # A skew-detected write is immediately followed by `close_listener`
        # and (synchronously, same thread) `drain` -> possibly `os._exit(0)`
        # -- an unflushed buffered write would never reach the pipe before
        # the process dies. Every write on this path flushes explicitly
        # rather than relying on `io.close()`'s implicit flush, which may
        # never run for exactly that reason.
        io.write(data)
        io.flush()

    try:
        try:
            line = io.readline()
        except OSError:
            return
        if not line:
            return
        _serve_line(
            line,
            write=_write_flushed,
            version_state=version_state,
            server_sha=server_sha,
            close_listener=close_listener,
            drain=drain,
            release_in_flight=_exit_once,
            dispatch=dispatch,
            mark_invocation=mark_invocation,
            record_invocation=record_invocation,
            record_exit=record_exit,
        )
    finally:
        _exit_once()
        try:
            io.close()
        except OSError:
            pass


def _create_pipe_instance(name: str, sid: str) -> int:
    """Create one FOLLOW-ON pipe instance (never the first -- that is
    election's own job) carrying the same restricted ACL as the elected
    first instance. See module docstring's negative-spec for why this
    reaches into `election._build_security_attributes` rather than
    duplicating the SDDL descriptor.
    """
    import ctypes
    import _winapi

    from coordinator_core.warm.election import _build_security_attributes

    security_attributes = _build_security_attributes(sid)
    return _winapi.CreateNamedPipe(
        name,
        _winapi.PIPE_ACCESS_DUPLEX,
        _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT,
        _winapi.PIPE_UNLIMITED_INSTANCES,
        _PIPE_BUFFER_BYTES,
        _PIPE_BUFFER_BYTES,
        0,
        ctypes.addressof(security_attributes),
    )


def _connect_pipe(handle: int) -> None:
    """Block until a client connects to `handle`. `ERROR_PIPE_CONNECTED`
    (a client already connected before this call was posted) is a win, not
    a failure -- see `_ERROR_PIPE_CONNECTED`'s own comment."""
    import _winapi

    try:
        _winapi.ConnectNamedPipe(handle, _winapi.NULL)
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_PIPE_CONNECTED:
            raise


def _close_handle(handle: int) -> None:
    import _winapi

    try:
        _winapi.CloseHandle(handle)
    except OSError:
        pass


def _wrap_handle(handle: int) -> Any:
    """Wrap a connected pipe HANDLE into the same blocking file-object API
    the client already reads/writes (`warm.client._open_pipe`), via
    `msvcrt.open_osfhandle` + `os.fdopen` -- verified end-to-end on this
    box before landing (throwaway probe, discarded per this repo's probe
    discipline)."""
    import msvcrt

    fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    return os.fdopen(fd, "r+b")


class _ServerContext:
    """Boot-scoped server state: the pipe name, the ACL identity, the
    version state constructed once, the in-flight counter every connection
    thread shares, this server life's `warm.telemetry.ServerTelemetry`, and
    the idle-demotion watchdog.

    `close_listener` / `_drain` are the two callables `_serve_line` binds
    into `warm.skew.evict_on_skew` for the skew-eviction path; the SAME
    `close_listener` and a `begin_shutdown`-bound `_idle_tick` are what the
    idle-demotion watchdog below uses -- both triggers share the single
    `warm.lifecycle` shutdown guard, so whichever fires first is the only
    one that ever reaches `_ctx_shutdown`/`os._exit`.

    `engine_root` is threaded through only so `_ctx_shutdown` can unlink
    THIS clone's breadcrumb (`warm.breadcrumb.svc_dir`'s own per-clone
    resolution) rather than assuming the caller's cwd.
    """

    def __init__(
        self,
        *,
        name: str,
        sid: str,
        version_state: "skew.ServerVersionState",
        engine_root: Optional[Path] = None,
        boot_token: Optional[str] = None,
    ):
        self.name = name
        self.sid = sid
        self.version_state = version_state
        self.engine_root = engine_root
        # The generation token this server's pipe name was built from
        # (`main()` computes it once and passes it here rather than having
        # this class re-derive it, so the retirement predicate compares
        # against the token actually EMBEDDED in `self.name` and cannot
        # drift from it). `None` disables the superseded arm entirely --
        # the shape every test that constructs a context directly gets,
        # keeping the idle watchdog's behaviour unchanged for them.
        self.boot_token = boot_token
        self.in_flight = InFlightCounter()
        self.telemetry = telemetry.ServerTelemetry()
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._listening_lock = threading.Lock()
        self._listening = True
        self._stopped = threading.Event()
        self._idle_watchdog_stop = threading.Event()
        # Lazily constructed -- see `_ensure_dispatch_pool`. Left `None`
        # until a real worker thread actually dispatches through it, so a
        # test that constructs a `_ServerContext` (or even starts the
        # worker-thread pool) without ever letting a request reach
        # `_pool_dispatch` -- the common shape across this module's own
        # test suite, which monkeypatches `_handle_connection` directly --
        # never pays for real OS process spawns it does not exercise.
        self._dispatch_pool: Optional["concurrent.futures.ProcessPoolExecutor"] = None
        self._dispatch_pool_lock = threading.Lock()

    def close_listener(self) -> None:
        with self._listening_lock:
            self._listening = False

    def _is_listening(self) -> bool:
        with self._listening_lock:
            return self._listening

    def record_invocation(self, warm: bool) -> None:
        self.telemetry.record_invocation(warm=warm)

    def record_exit(self, reason: str) -> None:
        self.telemetry.record_exit(reason)

    def _ensure_dispatch_pool(self) -> "concurrent.futures.ProcessPoolExecutor":
        """Build the `DISPATCH_PROCESS_POOL_SIZE`-worker-process pool on
        first use, double-checked-locking style, so concurrent worker
        THREADS racing to dispatch their first request each see the SAME
        pool rather than each starting their own.
        """
        if self._dispatch_pool is None:
            with self._dispatch_pool_lock:
                if self._dispatch_pool is None:
                    self._dispatch_pool = concurrent.futures.ProcessPoolExecutor(
                        max_workers=DISPATCH_PROCESS_POOL_SIZE,
                        initializer=_worker_process_init,
                    )
        return self._dispatch_pool

    def _pool_dispatch(self, msg: dict, *, session_id: Optional[str] = None) -> dict:
        """The `dispatch=` callable a real accept-loop worker thread
        (`_worker_loop`) hands to `_handle_connection` -- submits the call
        to `DISPATCH_PROCESS_POOL_SIZE` worker PROCESSES and blocks this
        connection's own thread for the result, exactly where `_run_dispatch`
        used to run dispatch in-process. See `DISPATCH_PROCESS_POOL_SIZE`'s
        own comment for why this is the fix C1's Arm A evidence names: GIL
        contention inside `dispatch_message` cannot be removed by any
        in-process threading restructure, only by moving the CPU-bound work
        off this process's interpreter lock entirely.
        """
        try:
            future = self._ensure_dispatch_pool().submit(_pool_dispatch_worker, msg, session_id)
            return future.result()
        except BrokenProcessPool:
            # A ProcessPoolExecutor whose worker died is broken PERMANENTLY --
            # every later submit() on that instance raises, so without this the
            # first dead worker turns a resident server into one that fails
            # every request it will ever receive, for its whole 15-minute idle
            # life. Observed live 2026-08-19: a published server served
            # BrokenProcessPool to `ping` itself, and only a hard kill cleared
            # it. That silently violated this module's own NEVER FAIL A CALLER
            # contract, which the pool was never exempt from.
            #
            # Drop the corpse so the next request rebuilds a fresh pool.
            with self._dispatch_pool_lock:
                broken = self._dispatch_pool
                self._dispatch_pool = None
            if broken is not None:
                try:
                    broken.shutdown(wait=False)
                except Exception:
                    pass

            # A dead worker's `future.result()` raises BrokenProcessPool for
            # its OWN future too, not only for later submissions -- a worker
            # that crashed mid-dispatch may have already PERFORMED the op
            # (mutation included) before dying with the result unsent. For a
            # COMPUTE_ONLY op that ambiguity is free to resolve by re-running:
            # degrading to the pre-C6 GIL-bound path costs latency under
            # concurrency and nothing else (C1's measurement is why the pool
            # exists, not a reason to prefer a failed dispatch over a slow
            # one). For a MUTATING op it is not free -- re-running here would
            # be the server unilaterally re-executing a possibly-already-done
            # mutation, the exact double-execution class this module's own
            # per-request-state docstring and `warm.client`'s delivered-then-
            # ambiguous ladder both exist to prevent (a `git commit` that ran
            # twice under two Commit-Tokens, 2026-08-19). Return the honest
            # refusal instead; `warm.client`'s pass-through surfaces it to the
            # caller unchanged, same as a client-detected indeterminate case.
            if _op_may_mutate(msg.get("method")):
                return _pool_broken_indeterminate_envelope(msg)
            return _run_dispatch(msg, session_id=session_id)

    def _ctx_shutdown(self) -> None:
        """Step 3 of `warm.lifecycle`'s sequence: flush the log, unlink the
        breadcrumb, close pipe handles. `os._exit(0)` (step 4, run
        immediately after this by `lifecycle._run_tail`) reclaims every
        open pipe handle at the OS level regardless, so this step's own
        work is exactly the two on-disk artifacts this server life owns --
        never raises, per both `telemetry.flush()`'s and
        `breadcrumb.unlink_breadcrumb()`'s own "never raises" contracts.
        """
        self._idle_watchdog_stop.set()
        self.telemetry.flush(engine_root=self.engine_root)
        # Ownership-checked: a superseded generation reaching this point
        # is exiting while its SUCCESSOR owns the clone's single
        # breadcrumb, and an unconditional unlink would delete the live
        # successor's entry. See `breadcrumb.unlink_breadcrumb`.
        breadcrumb.unlink_breadcrumb(self.engine_root, owner_pid=os.getpid())
        if self._dispatch_pool is not None:
            # Best-effort, mirroring every other step in this method's own
            # "never raises" contract -- `os._exit(0)` (lifecycle's next
            # step) reclaims the worker processes at the OS level regardless
            # of whether this shutdown call itself completes cleanly.
            try:
                self._dispatch_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
        return None

    def _drain(self) -> None:
        lifecycle.drain_and_exit(in_flight_count=self.in_flight, ctx_shutdown=self._ctx_shutdown)

    def _token_is_stale(self) -> bool:
        """`warm.idle.TokenStaleFn`: has a newer engine generation
        superseded this one?

        Compares this server's boot token -- the one embedded in the pipe
        name it owns -- against a live `skew.compute_client_token` read.
        A mismatch means every client now computes a different pipe name,
        so this process is unreachable and `skew.evict_on_skew` can never
        fire for it (see `warm.idle`'s SUPERSEDED-GENERATION PREDICATE).

        Never raises: this runs on the idle watchdog thread every poll,
        and a transient stat failure while the publish round is mid-write
        must not kill the watchdog. An unreadable token reads as NOT
        stale, so the failure mode is "waits out the ordinary idle
        deadline," which is exactly today's behaviour -- never a false
        retirement of a healthy server.

        NEGATIVE SPEC -- unstamped (live working tree) `engine_root` is
        deliberately IN SCOPE, not a bug: `skew.compute_client_token`
        falls back to a `.git/HEAD` fingerprint there, which rotates on
        this box's shared-branch cadence (~30-40s) independent of whether
        engine code changed, so this predicate flips True that often and
        the server retires within one watchdog poll. That is not a false
        retirement in the sense that matters: the same token rotation
        that trips this predicate already changed the pipe name every
        client dials, so the old server is ALREADY unreachable by the
        time it retires -- its residency was useless residency, and early
        retirement costs zero warm hits. Gating `token_stale` on stamp
        presence (i.e. only binding it when a stamp exists) was
        considered and rejected: the unstamped population left unfixed by
        that gate is the live/dev-clone shape, whose steady-state stranded
        count is WORSE than the published-mirror case this predicate was
        written for -- a ~30-40s rotation cadence against a 15-minute idle
        deadline implies on the order of 25-30 resident generations, where
        a publish cadence implies a handful. That arithmetic is derived
        from the measured rotation cadence, not itself an observed count.
        """
        if self.boot_token is None:
            return False
        try:
            return skew.compute_client_token(self.engine_root) != self.boot_token
        except Exception:  # noqa: BLE001 -- see docstring; a read failure is not a verdict
            return False

    def _idle_tick(self) -> None:
        """One watchdog poll: record why (if this poll is the one that
        demotes) before handing off to `idle.demote_if_idle`, which owns
        the actual predicate and the `begin_shutdown` call.

        `_token_is_stale()` is read ONCE here and threaded through to both
        `should_demote` and `demote_if_idle` as a captured constant, rather
        than each call re-reading live state: `_token_is_stale` swallows
        stat failures as False, so a transient failure (e.g. a stamp file
        mid-rewrite) landing between separate reads could make one read
        True and another False, misattributing the recorded exit reason
        (EXIT_REASON_SUPERSEDED vs EXIT_REASON_IDLE_DEMOTION) for a server
        that only actually saw one verdict. One tick must observe one
        consistent verdict.
        """
        token_stale = self._token_is_stale()
        if idle.should_demote(
            served_count=self.telemetry.served_count,
            token_stale=lambda: token_stale,
        ):
            # Which arm fired decides the recorded reason, and the two are
            # not interchangeable in the telemetry record -- see
            # `warm.telemetry`'s EXIT_REASON_SUPERSEDED note. Use the
            # captured verdict rather than inferring from `should_demote`:
            # a superseded server is usually ALSO past some deadline, so
            # the arms overlap and only the specific read distinguishes them.
            self.record_exit(
                telemetry.EXIT_REASON_SUPERSEDED
                if token_stale
                else telemetry.EXIT_REASON_IDLE_DEMOTION
            )
        idle.demote_if_idle(
            served_count=self.telemetry.served_count,
            token_stale=lambda: token_stale,
            close_listener=self.close_listener,
            in_flight_count=self.in_flight,
            ctx_shutdown=self._ctx_shutdown,
        )

    def _idle_watchdog_loop(self) -> None:
        """Runs on its OWN thread, independent of the accept loop -- the
        whole point (module docstring: idle demotion "must fire even when
        the accept loop never receives a single connection"). `Event.wait`
        both sleeps and gives `_ctx_shutdown` a way to end this thread
        promptly once some OTHER trigger has already won the shutdown
        guard, rather than polling `idle.should_demote` needlessly past
        that point (harmless either way -- `demote_if_idle` no-ops once
        `lifecycle`'s single-shot guard is spent -- but there is no reason
        to keep ticking).
        """
        while not self._idle_watchdog_stop.wait(_IDLE_WATCHDOG_POLL_SECS):
            self._idle_tick()

    def serve_forever(self, first_handle: int) -> None:
        """Kick off `WORKER_POOL_SIZE` bounded dispatch workers,
        `PENDING_LISTENER_POOL_SIZE` independent self-replenishing accept
        chains -- `first_handle` (election's own instance) plus
        `PENDING_LISTENER_POOL_SIZE - 1` further instances created here --
        and the idle watchdog, each on its own thread, then block for the
        life of the process.

        Workers are started FIRST so the queue has consumers the instant
        the first connection is accepted -- an accept chain never blocks on
        a full queue either way (`queue.Queue()` here is unbounded, per
        `_enqueue_connection`'s own docstring), but starting workers first
        avoids an avoidable warm-up gap between "accepted" and "a worker is
        available to notice."

        Each chain already replenishes itself one-for-one on every accept
        (`_accept_and_replenish`'s own docstring); the only change this row
        makes is starting with N chains instead of one, which is what turns
        "exactly one pending listener, always" into "exactly
        `PENDING_LISTENER_POOL_SIZE` pending listeners, always" (problem 3).
        Boot-time pool creation is best-effort: a `_create_pipe_instance`
        failure here shrinks the pool by one and is logged, never raised --
        a smaller-than-intended pool degrades toward today's one-listener
        behaviour rather than killing a server that can otherwise serve.

        A drain triggered from any connection thread OR the idle watchdog
        ends the whole process via `os._exit(0)` (`warm.lifecycle`), which
        is the only thing that ever wakes this wait -- no separate
        interrupt mechanism is needed (module docstring's "TRANSPORT
        MODEL"). Pool instances still blocked in `_connect_pipe` at that
        point are never joined or cancelled -- same as the single pending
        instance today -- `os._exit(0)` reclaims every open handle at the
        OS level regardless (`_ctx_shutdown`'s own docstring), and none of
        them are counted in `InFlightCounter`, so a pool of unconnected
        listeners can never stall the drain wait.
        """
        self._start_worker_pool()
        self._start_pending_listener_pool(first_handle)
        threading.Thread(target=self._idle_watchdog_loop, daemon=True).start()
        self._stopped.wait()

    def _start_worker_pool(self, *, pool_size: int = WORKER_POOL_SIZE) -> None:
        """Start `pool_size` long-lived dispatch worker threads, each
        running `_worker_loop` forever. Split out of `serve_forever` for
        the same isolated-exercise reason `_start_pending_listener_pool`
        is: a boot-time step that should be drivable in a test without also
        blocking on `self._stopped.wait()`.
        """
        for _ in range(pool_size):
            threading.Thread(target=self._worker_loop, daemon=True).start()

    def _worker_loop(self) -> None:
        """One bounded dispatch worker's whole life: block on the shared
        queue, hand the next `io` off to `_handle_connection`, repeat.
        `already_entered=True` because `_enqueue_connection` already
        claimed this request's `in_flight` slot -- see that method's and
        `_handle_connection`'s own docstrings for why a second `enter()`
        here would double-count the slot. A wedged dispatch stalls only
        this one worker thread (module docstring's TRANSPORT MODEL note);
        the other `pool_size - 1` workers keep draining the queue.
        """
        while True:
            io = self._queue.get()
            _handle_connection(
                io,
                version_state=self.version_state,
                server_sha=self.version_state.server_sha,
                close_listener=self.close_listener,
                drain=self._drain,
                in_flight=self.in_flight,
                dispatch=self._pool_dispatch,
                record_invocation=self.record_invocation,
                record_exit=self.record_exit,
                already_entered=True,
            )

    def _enqueue_connection(self, io: Any) -> None:
        """Claim this connection's `in_flight` slot and hand its `io` off
        to the shared queue -- the accept-and-queue boundary's ENQUEUE
        point the module docstring's DRAIN SEMANTICS names. Called from an
        accept-chain thread (`_accept_and_replenish`), never from a worker
        thread. `queue.Queue()` is unbounded here, so this never blocks the
        accept chain -- acceptance stays bounded by
        `PENDING_LISTENER_POOL_SIZE` alone, dispatch by `WORKER_POOL_SIZE`
        alone, and neither bound can starve the other.
        """
        self.in_flight.enter()
        self._queue.put(io)

    def _start_pending_listener_pool(
        self, first_handle: int, *, pool_size: int = PENDING_LISTENER_POOL_SIZE
    ) -> None:
        """Start `first_handle`'s accept chain plus `pool_size - 1` further
        ones, each on its own daemon thread. Split out of `serve_forever`
        so this boot-time pool-creation step is exercisable in isolation
        (`test_server_loop.py`) without also driving the idle watchdog or
        blocking on `self._stopped.wait()`. See `serve_forever`'s own
        docstring for the invariant this establishes and the shutdown
        interaction.
        """
        threading.Thread(target=self._accept_and_replenish, args=(first_handle,), daemon=True).start()
        for _ in range(pool_size - 1):
            try:
                extra_handle = _create_pipe_instance(self.name, self.sid)
            except OSError as exc:
                print(f"[warm-server] failed to create pool instance at boot: {exc!r}", file=sys.stderr)
                continue
            threading.Thread(
                target=self._accept_and_replenish, args=(extra_handle,), daemon=True
            ).start()

    def _accept_and_replenish(self, handle: int) -> None:
        """Block for a connection on `handle`, then -- BEFORE handling that
        connection -- post a fresh instance's connect-wait on ITS OWN
        thread, and only then hand `handle` off to `_handle_connection` on
        THIS thread.

        Ordering is the whole point: posting the replacement first is what
        keeps a pending instance listening continuously rather than only
        after a connection has already been wrapped and enqueued. A
        fully-serial create -> connect -> enqueue -> repeat loop leaves a
        real (if narrow -- CreateNamedPipe is a single syscall) window
        between one instance being claimed and the next being posted,
        during which a simultaneous client sees FileNotFoundError instead
        of ERROR_PIPE_BUSY -- still a handled outcome on the client's own
        anti-storm table (`warm.client`'s own docstring: FileNotFoundError
        is "THE ONLY SPAWN TRIGGER, then cold"), but narrowing it costs
        nothing here since replenishment is a single fast syscall dispatched
        to its own thread rather than serialized after this connection's
        full enqueue step.

        This thread's own job ends at enqueue (`_enqueue_connection`) --
        dispatch happens on one of the `WORKER_POOL_SIZE` bounded worker
        threads instead, per the accept-and-queue split the module
        docstring's DRAIN SEMANTICS section names. Before this row, this
        thread called `_handle_connection` directly, which is exactly the
        "one handler thread per accepted connection, unbounded" shape AC7
        forbids -- accepting fast under load meant spawning dispatch
        threads without limit; `WORKER_POOL_SIZE`'s own constant docstring
        has the full accounting.
        """
        try:
            _connect_pipe(handle)
        except OSError as exc:
            print(f"[warm-server] connect failed on pipe instance: {exc!r}", file=sys.stderr)
            _close_handle(handle)
            return

        if not self._is_listening():
            _close_handle(handle)
            return

        if self._is_listening():
            try:
                next_handle = _create_pipe_instance(self.name, self.sid)
            except OSError as exc:
                print(f"[warm-server] failed to create pipe instance: {exc!r}", file=sys.stderr)
            else:
                threading.Thread(
                    target=self._accept_and_replenish, args=(next_handle,), daemon=True
                ).start()

        io = _wrap_handle(handle)
        self._enqueue_connection(io)


def _self_stable_pid_start_epoch() -> Optional[int]:
    """This process's own birth instant, in the SAME derivation
    `coordinator_core.session.core.stable_pid_alive` compares a stored
    breadcrumb value against (`_win_create_time_epoch`'s own docstring:
    "the SAME comparison now used on both platforms"). Reached into
    directly, mirroring `_create_pipe_instance`'s reach into
    `election._build_security_attributes` (module docstring's negative
    -spec) -- a second, independently-derived epoch here risks disagreeing
    with the read side by exactly the tolerance window `stable_pid_alive`
    exists to police. Returns `None` if this process's own pid cannot be
    read (should not happen for a live self-lookup, but the breadcrumb
    writer treats it the same as any other unavailable field).
    """
    from coordinator_core.session.core import _win_create_time_epoch

    try:
        return _win_create_time_epoch(os.getpid())
    except Exception:
        return None


def main() -> int:
    """`SERVER_ENTRY_SCRIPT`'s process entrypoint. Boot sequence:

    1. Resolve this engine clone's pipe name using the SAME primary
       skew token the client computes (`skew.compute_client_token`) as the
       pipe's generation stamp -- a successor bound to a new commit
       computes a different token, hence a different pipe name, and binds
       immediately without contesting the predecessor's still-draining
       pipe (election.py's own docstring).
    2. `election.elect()` the first instance. `ElectionLost` means another
       process already won this exact generation's pipe -- not an error,
       just nothing for this process to do; exits 0.
    3. Construct `skew.ServerVersionState` ONCE (boot identity).
    4. Write the breadcrumb (`warm.breadcrumb.write_breadcrumb`) -- only
       reachable past step 2, so a process that lost the election never
       clobbers the winner's breadcrumb.
    5. Declare this process's execution route
       (`_declare_execution_route`), so its op-latency rows -- and its
       dispatch pool workers', which inherit the environment -- stamp
       `warm_server` instead of the `in_process` default.
    6. Run the accept loop until a drain-triggered `os._exit(0)` ends the
       process.
    """
    if sys.platform != "win32":
        print("[warm-server] this module is Windows-only", file=sys.stderr)
        return 1

    repo_root = _engine_clone_root()
    sid = election.current_user_sid()
    token = skew.compute_client_token(repo_root)
    name = election.pipe_name(token, engine_clone=repo_root, user_sid=sid)

    try:
        first_handle = election.elect(name, user_sid=sid)
    except election.ElectionLost:
        print(
            f"[warm-server] election lost for {name!r}; another server already "
            "owns this generation's pipe, exiting",
            file=sys.stderr,
        )
        return 0

    version_state = skew.ServerVersionState(repo_root)

    try:
        breadcrumb.write_breadcrumb(
            pipe=name,
            pid=os.getpid(),
            stable_pid_start_epoch=_self_stable_pid_start_epoch() or 0,
            engine_sha=version_state.server_sha,
            engine_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001 -- a HINT writer failing must not stop the server
        print(f"[warm-server] failed to write breadcrumb: {exc!r}", file=sys.stderr)

    _declare_execution_route()

    _suppress_pool_worker_consoles()

    _preload_op_registry()

    ctx = _ServerContext(
        name=name,
        sid=sid,
        version_state=version_state,
        engine_root=repo_root,
        boot_token=token,
    )
    ctx.serve_forever(first_handle)
    return 0


def _preload_op_registry() -> None:
    """Import the op registry at BOOT, so the first caller does not pay for it.

    `_run_dispatch` imports `coordinator_core.ipc.dispatch_message`
    function-locally, and the registry populates through it on first
    dispatch — inside the server, cached in `sys.modules` thereafter. That
    is ~316 `coordinator_core.ops.*` modules landing on the critical path of
    whichever caller happens to arrive first. Measured on the published
    mirror, 2026-08-19: **first dispatch 703 ms, every subsequent dispatch
    ~3 ms**.

    A one-time cost per server GENERATION, not per server lifetime — and
    generations are not rare. `skew.compute_client_token` keys on
    `(.git/HEAD, its ref)`, so on a live working tree the token rotates at
    the tree's commit cadence (measured ~2.6 min median on this box), and
    each new generation re-presents that 703 ms to its first caller. A
    server that is evicted early can spend most of its life having served
    warm-up.

    Moving it here puts it on a process nobody is waiting for, which is the
    entire point of having a resident server. The pipe is already bound by
    `election.elect` above, so a client arriving mid-preload waits exactly
    what it would have waited inside the dispatch — no caller is made worse
    off, and every caller after the first is made better.

    NEGATIVE-SPEC:
      - Runs AFTER the election, never before: a process that lost has
        nothing to serve and must not pay 700 ms to discover that.
      - Best-effort. A failed preload is a slow first dispatch, not a dead
        server, so this never raises past the caller — the registry will
        simply populate on first dispatch exactly as it did before.
      - Does NOT dispatch anything. Importing is what populates the
        registry (registration is an import side effect); a synthetic
        warm-up request would also mutate telemetry's served counts and
        make `served_count` lie about real traffic.
    """
    try:
        import coordinator_core.ops  # noqa: F401 -- registration is the side effect
        from coordinator_core.ipc import dispatch_message  # noqa: F401
    except Exception as exc:  # noqa: BLE001 -- a slow first call beats no server
        print(f"[warm-server] op-registry preload failed: {exc!r}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
