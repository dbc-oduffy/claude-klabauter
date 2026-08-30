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

PLATFORMS -- Windows named pipe, POSIX unix socket, ADDED 2026-08-21.
`main()` refused to start off Windows until that date, so a POSIX door had
no server to talk to and no Mac could run any of this. The two transports
now differ in exactly one boot step (`_elect_windows_pipe` /
`_elect_unix_socket_endpoint`) and one accept layer (`serve_forever` /
`serve_forever_unix`). EVERYTHING ELSE -- the queue, the bounded worker
pool, `InFlightCounter`, the idle watchdog, the skew/eviction path, the
breadcrumb, telemetry, the dispatch process pool -- is one implementation
serving both, which is the property that keeps the POSIX arm from becoming
a second, slowly-diverging server. Read `warm.election`'s own docstring
before touching the POSIX election: POSIX has no
`FILE_FLAG_FIRST_PIPE_INSTANCE`, and the stale-socket reclaim that stands
in for it is the one place the two platforms are not merely different but
unequal in what the kernel guarantees.

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
line` pops the top-level `_caller` object off the request, resolves it via
`caller_context.resolve_caller_context`, and threads its `session_id` through
-- docs/plans/2026-08-30-every-op-runs-in-the-callers-environment.md § C1b) --
this server process's OWN environment (whoever spawned it) must never leak into
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
from typing import Any, Callable, Mapping, NamedTuple, Optional

from coordinator_core.ipc import INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR
from coordinator_core.telemetry import op_latency
from coordinator_core.telemetry import spawn_counter as _spawn_counter
from coordinator_core.warm import (
    breadcrumb,
    caller_context,
    election,
    idle,
    lifecycle,
    push_cadence,
    settings_home_claim,
    skew,
    telemetry,
)
from coordinator_core.warm.caller_context import CallerContext
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
    "ACCEPTOR_POOL_SIZE",
    "UNTRUSTED_CALLER_ERROR",
    "SETTINGS_HOME_MISMATCH_ERROR",
    "main",
]

# UNTRUSTED CALLER ERROR -- a request carrying no `_engine_token` at all.
# Distinct from `skew.ENGINE_SKEW` (-32002, a PRESENT token that disagrees
# with this server's live one) and from `ipc.STRUCTURAL_PIN_ERROR` (-32001):
# neither fires here, because there is no token to compare. Next free slot
# in the app-defined range JSON-RPC 2.0 §5.1 reserves (`ipc.py`'s own
# comment); `WARM_DISPATCH_INDETERMINATE` already claimed -32004.
UNTRUSTED_CALLER_ERROR = -32003

# SETTINGS HOME MISMATCH -- a request whose caller explicitly named a
# `COORDINATOR_SETTINGS_HOME` this server does not serve. Next free slot after
# -32007 (`ipc.ENTRYPOINT_NOT_WARM_LOADABLE_ERROR`); mirrored in
# `warm/door/door_core.h` as `JSONRPC_SETTINGS_HOME_MISMATCH`, which is what lets
# the native door classify it as provably undispatched and run the call cold.
# Distinct from every code above it in kind: those are statements about the
# REQUEST (unparseable, untrusted, skewed, not warm-loadable); this one is a
# statement about THIS SERVER -- the request is well-formed and authorized, and
# this process simply is not the one that can answer it.
SETTINGS_HOME_MISMATCH_ERROR = -32008

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

# ACCEPTOR POOL SIZE -- the POSIX accept layer's own bound
# (`_ServerContext._start_acceptor_pool`). Reuses
# `PENDING_LISTENER_POOL_SIZE` rather than deriving a second number,
# because it answers the same question that constant answers on Windows:
# how many simultaneous arrivals should this server be able to take off
# the kernel at once. It is NOT the same THING, and reading it as one
# would misprice both. A Windows pending listener is a kernel pipe
# instance plus a blocked thread, and its count caps how many clients can
# connect at all before they see a busy error. A POSIX acceptor is only a
# blocked thread: the kernel's own `listen()` backlog
# (`election.UNIX_LISTEN_BACKLOG`, 128) is what caps simultaneous
# arrivals, and this constant caps only how fast they are drained off it
# into the shared queue. Sized alike because the load norm behind both is
# the same one (docs/wiki/machine-load-norm.md's "as though 30 callers are
# queued"); named separately because the cost per unit and the failure
# mode at the bound are not.
ACCEPTOR_POOL_SIZE = PENDING_LISTENER_POOL_SIZE


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


def _spawn_count_or_none() -> Optional[int]:
    """Process-local spawn count, or ``None`` on any failure.

    Mirrors `coordinator_core.ipc._spawn_count_or_none`'s shape (try/except,
    `None`-not-`0` on failure) for the two `warm/server.py` telemetry sites
    below. `spawn_counter.spawn_count()` is documented never to raise, but
    that safety lives in `spawn_counter.py`'s own contract, not here -- wrap
    for defense-in-depth so a future violation of that contract costs one
    telemetry row, never a dispatch.
    """
    try:
        return _spawn_counter.spawn_count()
    except Exception:
        return None


def _spawn_delta(start: Optional[int], end: Optional[int]) -> Optional[int]:
    """Spawns between two readings, or ``None`` if either end is unavailable.

    Mirrors `coordinator_core.ipc._spawn_delta`. Trusts, without re-asserting,
    that `spawn_counter`'s own negative spec holds ("not reset between ops,
    ever") -- if that global were ever reset mid-measurement this could
    return a negative delta; not a bug given that contract, but a dependency
    this function does not defend on its own.
    """
    if start is None or end is None:
        return None
    return end - start


def _run_dispatch(msg: dict, *, caller: Optional[CallerContext] = None, isolated: bool = False) -> dict:
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

    `caller`, when given (the caller's own resolved identity SET, carried
    over the wire as the request's top-level `_caller` object and popped
    and resolved into a `warm.caller_context.CallerContext` by `_serve_line`
    before this call -- docs/plans/2026-08-30-every-op-runs-in-the-callers-
    environment.md § C1b), has its `session_id` bound for the duration of
    the dispatch via `per_request_state`'s own `session_id` parameter -- see
    that seam's docstring for the full identity-attribution defect this
    closes. `None` (no identity carried) is a no-op bind, reproducing
    today's server-resolves-its-own-env behaviour exactly.

    `isolated` (C3, defaults `False`) is threaded straight through to
    `entry_seam.per_request_state`'s own required `isolated` argument. This
    function runs on the accept process's own connection thread -- never in
    a process no other in-flight request shares -- so `False` is correct for
    every caller EXCEPT none in production today: the real accept loop
    dispatches through `_ServerContext._pool_dispatch` (which submits to the
    process pool instead), and that method's own `BrokenProcessPool`
    fallback -- the one production caller of this function -- passes
    `isolated=False` explicitly, matching this default, because that fallback
    still executes in THIS shared process.

    STDOUT CAPTURE (W9). A handler `print()` here is not the transport --
    unlike `invoke.__main__`'s cold path, this process's stdout is never the
    JSON-RPC channel (the pipe is). But left unredirected it either lands on
    whatever this process's own stdout happens to be bound to (a resident
    server normally has none, spawned detached) or, once
    `_suppress_pool_worker_consoles`/`_bind_null_std_streams` are in play,
    `os.devnull` -- silently discarded either way, the exact divergence from
    cold (`invoke.__main__` relays captured stdout to stderr) this row
    exists to close. `contextlib.redirect_stdout` captures it the same way
    the cold path does and folds it into the SAME `_stderr` sibling field
    `diagnostics` already rides -- `warm.client` pops and relays that field
    unconditionally, so no second wire-shape change is needed.
    """
    import asyncio
    import contextlib
    import io as _io
    import time as _time

    from coordinator_core.ipc import (
        MEASUREMENT_SCOPE_PROCESS_WIDE,
        dispatch_message,
        record_op_process_time,
        resolve_caller_cwd,
        resolve_request_repo,
    )

    diagnostics: list = []
    _handler_stdout = _io.StringIO()
    _handler_stderr = _io.StringIO()
    # C9: process-time measured on THIS thread only, but `WORKER_POOL_SIZE`
    # threads in this accept process share one interpreter and one
    # `time.process_time()` clock -- a delta taken here can include CPU spent
    # dispatching a DIFFERENT op on a sibling thread during the same
    # wall-clock span. That makes this figure process-wide, never this op's
    # own uncontaminated CPU (contrast `_pool_dispatch_worker` below, which
    # runs alone in its own process) -- recorded under
    # MEASUREMENT_SCOPE_PROCESS_WIDE so no consumer can mistake it for a
    # per-op figure. See `coordinator_core.ipc.record_op_process_time`'s own
    # docstring for the full rationale. The spawn-count delta below is
    # equally process-wide for the same reason -- `spawn_counter` is one
    # process-global counter, so a sibling thread's spawns during this same
    # window land inside this thread's delta too; a reader must apply this
    # row's own `measurement_scope` to `spawns` exactly as it does to
    # `process_ms`.
    _t_start = _time.time()
    _process_start = _time.process_time()
    _spawn_start = _spawn_count_or_none()
    _caller_route = "coordinator_core.warm.server._run_dispatch"
    session_id = caller.session_id if caller is not None else None
    try:
        with per_request_state(
            session_id=session_id, diagnostics=diagnostics, warm_served=True, isolated=isolated
        ):
            with contextlib.redirect_stdout(_handler_stdout), contextlib.redirect_stderr(_handler_stderr):
                response = asyncio.run(dispatch_message(msg, caller=_caller_route))
    finally:
        _process_ms = (_time.process_time() - _process_start) * 1000.0
        _repo_root = resolve_request_repo(msg) or resolve_caller_cwd(msg)
        method = msg.get("method") if isinstance(msg, dict) else None
        record_op_process_time(
            op=method if isinstance(method, str) else "<unknown>",
            process_ms=_process_ms,
            measurement_scope=MEASUREMENT_SCOPE_PROCESS_WIDE,
            source_path="accept_thread",
            t_start=_t_start,
            repo_root=_repo_root,
            sid=session_id or None,
            spawns=_spawn_delta(_spawn_start, _spawn_count_or_none()),
            caller=_caller_route,
        )

    # The op's diagnostic lines ride the TRANSPORT frame, never `result` — the
    # wire envelope is frozen (contract §2.1) and a setup error is defined to
    # carry no reason field inside it. `_stderr` is a sibling of `result`,
    # popped by `warm.client` before the response reaches any consumer, so
    # nothing downstream of the client can observe a shape a cold spawn lacks.
    # Without this the reason dies on the SERVER's stderr and a warm-served
    # refusal is mute — see `entry_seam`'s DIAGNOSTIC-axis note for the report
    # that found it. Captured stdout is appended after the existing
    # diagnostics lines, same field, same relay -- see this function's own
    # "STDOUT CAPTURE" note above.
    #
    # STDERR CAPTURE (C6, root cause 1). `emit_diagnostic`'s 3 call sites are
    # not the only place a well-formed-but-refusing op writes its diagnostic
    # sentence to `sys.stderr` -- 1512 other sites across the tree do the same
    # thing directly, and none of them will ever be migrated (see this
    # chunk's own body: bridging real stderr covers all 1512 without touching
    # any of them). Without this capture those sentences land on THIS
    # process's own stderr, which a resident, normally-detached warm server
    # has no reader for, and `cc_invoke.route_mutation`'s `RouteMutationError.
    # op_stderr` -- built from the child's captured stderr on the cold path --
    # is silently empty on the warm path instead. Folded into the SAME
    # `_stderr` sibling field as stdout and `diagnostics`, so `warm.client`'s
    # existing unconditional pop-and-relay (line ~777) needs no second wire
    # change to carry it through.
    _captured = _handler_stdout.getvalue()
    _captured_stderr = _handler_stderr.getvalue()
    _stderr_lines = list(diagnostics)
    if _captured:
        _stderr_lines.append(_captured)
    if _captured_stderr:
        _stderr_lines.append(_captured_stderr)
    if _stderr_lines and isinstance(response, dict):
        response = {**response, "_stderr": "\n".join(_stderr_lines)}
    return response


def _pool_dispatch_worker(msg: dict, caller: Optional[CallerContext]) -> dict:
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

    Opens `per_request_state(..., isolated=True)` UNCONDITIONALLY (C3) --
    unlike `_run_dispatch`, this function has no other production shape:
    every call runs inside a freshly-dispatched worker process no concurrent
    request shares, on both transport legs (Windows named pipe, POSIX
    socket), so the `os.environ` identity mirror `isolated=True` opens is
    always safe here and never conditional on a caller-supplied flag.

    STDOUT CAPTURE (W9). This worker's own stdout is bound to `os.devnull`
    by `_bind_null_std_streams` (`_worker_process_init`, `pythonw.exe` spawn)
    -- a handler `print()` here is silently discarded, not merely
    unobserved, unless captured before that binding matters. Same
    `contextlib.redirect_stdout` capture as `_run_dispatch`'s own "STDOUT
    CAPTURE" note, folded into the same `_stderr` sibling field.
    """
    import asyncio
    import contextlib
    import io as _io
    import time as _time

    from coordinator_core.ipc import (
        MEASUREMENT_SCOPE_PER_OP_PROCESS,
        dispatch_message,
        record_op_process_time,
        resolve_caller_cwd,
        resolve_request_repo,
    )

    diagnostics: list = []
    _handler_stdout = _io.StringIO()
    _handler_stderr = _io.StringIO()
    # C9: this function runs entirely inside a `ProcessPoolExecutor` worker
    # process, one task at a time by the pool's own contract (this
    # function's own docstring above) -- a `time.process_time()` delta taken
    # around the dispatch call is that op's own CPU, uncontaminated by any
    # peer (peers run in SEPARATE worker processes). Recorded under
    # MEASUREMENT_SCOPE_PER_OP_PROCESS -- contrast `_run_dispatch` above,
    # whose accept-process threads share one interpreter and one clock.
    _t_start = _time.time()
    _process_start = _time.process_time()
    _spawn_start = _spawn_count_or_none()
    _caller_route = "coordinator_core.warm.server._pool_dispatch_worker"
    session_id = caller.session_id if caller is not None else None
    try:
        with per_request_state(
            session_id=session_id, diagnostics=diagnostics, warm_served=True, isolated=True
        ):
            with contextlib.redirect_stdout(_handler_stdout), contextlib.redirect_stderr(_handler_stderr):
                response = asyncio.run(dispatch_message(msg, caller=_caller_route))
    finally:
        _process_ms = (_time.process_time() - _process_start) * 1000.0
        _repo_root = resolve_request_repo(msg) or resolve_caller_cwd(msg)
        method = msg.get("method") if isinstance(msg, dict) else None
        record_op_process_time(
            op=method if isinstance(method, str) else "<unknown>",
            process_ms=_process_ms,
            measurement_scope=MEASUREMENT_SCOPE_PER_OP_PROCESS,
            source_path="pool_worker",
            t_start=_t_start,
            repo_root=_repo_root,
            sid=session_id or None,
            spawns=_spawn_delta(_spawn_start, _spawn_count_or_none()),
            caller=_caller_route,
        )

    # STDERR CAPTURE (C6) -- same rationale as `_run_dispatch`'s own note:
    # this worker's real `sys.stderr` (bound to `os.devnull` by
    # `_bind_null_std_streams` under a `pythonw.exe` spawn) is where the
    # other 1512 non-`emit_diagnostic` sites write, and it has no reader
    # unless captured here.
    _captured = _handler_stdout.getvalue()
    _captured_stderr = _handler_stderr.getvalue()
    _stderr_lines = list(diagnostics)
    if _captured:
        _stderr_lines.append(_captured)
    if _captured_stderr:
        _stderr_lines.append(_captured_stderr)
    if _stderr_lines and isinstance(response, dict):
        response = {**response, "_stderr": "\n".join(_stderr_lines)}
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


def _untrusted_caller_response(request_id) -> dict:
    """JSON-RPC 2.0 error envelope for a request that carried no
    `_engine_token` field at all.

    THE GAP THIS CLOSES. `_engine_token is not None and version_state.
    is_skewed(...)` (`_serve_line`, below) skipped the skew check entirely
    for a tokenless request -- served by whatever generation happened to be
    listening, however stale, with no comparison ever attempted. Found
    empirically 2026-08-21: a hand-written client that omitted the field
    was served a `ping` without complaint. Every in-tree caller
    (`warm.client.engine_token`) always stamps a token -- it falls back to
    the literal string `"unversioned"` rather than omitting the field on
    any failure to compute one -- so this was unreachable from any traffic
    this box has ever sent itself; it stops being latent the moment a
    caller outside this module's own client (e.g. a non-Python door) speaks
    the wire protocol without stamping one. Ruling this responds to is the
    same one `skew.UnstampedEngineRootError` already enforces one layer up
    ("an engine root is a stamped build; no stamp, no engine"): a request
    with no way to prove its generation gets no default trust.

    NOT `skew.evict_on_skew`. A tokenless request is evidence about the
    CALLER, not about this server's own generation -- treating it as
    skew would run `close_listener` + `drain`, tearing down the resident
    server this whole shared box depends on, on the word of any single
    anonymous request. That turns a missing field into a one-line remote
    kill switch for every session sharing this pipe. This returns a refusal
    over the SAME connection and nothing more: the listener stays open, no
    other connection is affected, matching module docstring's "NEVER FAIL A
    CALLER" for this caller specifically, while still never dispatching to
    `coordinator_core.ipc.dispatch_message` on its behalf.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": UNTRUSTED_CALLER_ERROR,
            "message": (
                "warm dispatch refused: request carried no _engine_token. "
                "Dispatch through coordinator_core.warm.client, or stamp "
                "skew.compute_client_token() yourself before sending."
            ),
        },
    }


def _settings_home_refusal(request_id, claim: str, resolved: str) -> dict:
    """JSON-RPC 2.0 error envelope for a request that named a settings home
    this server does not serve.

    WHAT IS BEING REFUSED, AND WHY REFUSAL IS THE ANSWER. This server resolved
    `settings_home()` ONCE, out of the environment of whoever spawned it, and
    its identity key is (user, engine-clone, engine-token) -- the settings home
    is not in it. A request naming a different home therefore had exactly two
    possible outcomes before this check existed, and both were silent: the op
    read the WRONG home and returned a correct-looking result, or it WROTE to
    the wrong home. Verified 2026-08-29 through both `coordinator-invoke.cmd`
    and `coordinator-invoke.exe` -- `fleet.mode_show` reporting `fleet_value:
    null` while the overridden home held a set record -- and the diagnosis run
    that found it wrote a fleet-wide advisory suppression into the REAL shared
    home of a box carrying ~50 live sessions
    (state/bug-backlog/2026-08-29-the-warm-server-answers-against-its-spaw-
    f1bcc4154ca4.yaml).

    NOT AN ADVISORY-STATE PROBLEM. `bash_guards/_blanket_disarm.py ::
    marker_path()` resolves the blanket-disarm marker -- a file whose PRESENCE
    turns guards off -- through `settings_home()`, `authz/classification.py`
    keys op authorization off it, and `secrets/` is a directory inside it. A
    silently-wrong home on those paths answers in the direction that DISARMS,
    which is why this is a refusal rather than a warning, and why the posture
    matches the guard directories' own stated default on ambiguity: deny.

    BEFORE `dispatch`, AND THAT IS THE LOAD-BEARING PART. `_serve_line` runs
    this check ahead of the dispatch call, so a refused request provably never
    reached `coordinator_core.ipc.dispatch_message` and cannot have mutated
    anything. That is what earns -32008 its place in `door_core.c ::
    is_provably_undispatched`, which lets the native door fall through and run
    the call COLD -- in the caller's own process, where `settings_home()`
    resolves the home the caller actually named. The refusal is the honest
    answer on this side of the pipe; the cold leg is the working one.

    NOT `skew.evict_on_skew`, for the same reason `_untrusted_caller_response`
    is not: a caller naming another home is evidence about the CALLER's
    environment, not about this server's generation. Tearing down the resident
    server every session on this box shares, on the word of one request's env
    var, would turn an env var into a remote kill switch.

    NOT A FIX FOR THE UNDERLYING DEFECT, said plainly: a warm server still
    serves exactly one settings home, and callers that want another still do
    not get warm service. Resolving the home per REQUEST -- following
    `warm/caller_context.py`'s payload-first shape -- is the second step of
    that row's disposition, plan-sized because ~30 non-test callers of
    `settings_home()` need auditing for import-time resolution. This chunk
    converts a silent wrong answer into a visible one; it does not deliver
    isolation and must not be read as having done so.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": SETTINGS_HOME_MISMATCH_ERROR,
            "message": settings_home_claim.mismatch_message(claim, resolved),
        },
    }


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
    record_exit: Callable[..., None] = lambda reason, detail=None: None,
    record_served_repo: Callable[[Any], None] = lambda repo_root: None,
) -> None:
    """Process one request frame for one connection: write exactly one
    response frame (or delegate that write to `warm.skew.evict_on_skew`'s
    own `respond` callable on a detected skew), then release this request's
    in-flight slot. Never raises -- see module docstring's "NEVER FAIL A
    CALLER".

    A frame with no `_engine_token` at all is refused via
    `_untrusted_caller_response` BEFORE `version_state.is_skewed` is ever
    consulted -- a fail-CLOSED default distinct from the skew path itself:
    this does not close the listener or drain, it only refuses this one
    connection, so an untrusted caller cannot use its own missing token to
    evict a server every other session on the box is relying on. See
    `_untrusted_caller_response`'s own docstring for the full defect.

    `release_in_flight` is idempotency-guarded here (not by the caller) so
    it is safe to call it once inline (right after the response is written,
    which is what lets a skew-triggered `drain()` observe this request as
    already-released rather than deadlocking on its own count) and have the
    connection thread's own cleanup call it again as a no-op safety net.

    Pops `_caller` (the caller's own resolved identity SET, set by
    `warm.client._try_warm_dispatch_inner` and by `door.c`) off `msg` the
    same way `_engine_token` is already popped, resolves it into a
    `warm.caller_context.CallerContext` via `caller_context.
    resolve_caller_context`, and passes that object through to `dispatch`
    as a `caller=` kwarg -- `_run_dispatch`'s own docstring covers the
    bind/no-op contract from there. Absent entirely (older client, or a
    caller this transport cannot identify) resolves to a `CallerContext`
    whose `session_id`/`agent_id` are `None` (no ambient fallback for a
    per-call fact -- see `caller_context`'s own docstring), which is a
    no-op bind, not a fabricated identity. No deprecated top-level
    `_session_id` key is read any more (docs/plans/2026-08-30-every-op-
    runs-in-the-callers-environment.md § C1b) -- `door.c` and `warm.client`
    both send `_caller` now, and this repo publishes and installs both
    images in lockstep, so the mixed-version state an alias would defend
    against does not occur.

    Pops `_settings_home` (the caller's own resolved settings home, stamped
    by `warm.client._try_warm_dispatch_inner` and by the native door only
    when that caller EXPLICITLY set `COORDINATOR_SETTINGS_HOME`) and refuses
    the request outright when it names a home this server does not serve --
    after the skew check, before `dispatch`. Absent (no override in the
    caller's environment, which is every ordinary invocation) is not a
    mismatch and costs one `dict.pop`; see `_settings_home_refusal` for the
    defect and `warm/settings_home_claim.py` for why absence may never
    refuse.

    `mark_invocation` runs for EVERY frame this function is handed,
    including a skew-evicting one -- `warm.idle`'s own module docstring
    ("IDLE CLOCK OWNERSHIP") calls this out as deliberate, not a bug to
    suppress. `record_invocation` records a served (warm) request for
    `warm.telemetry`; `record_exit` records why this server is about to
    exit, called before `drain` on the skew path so `_ctx_shutdown`'s later
    `telemetry.flush()` observes the reason.

    `record_served_repo` is called, once per request that carries one, with
    the envelope's own `_origin_worktree` (`ipc.resolve_request_repo`) --
    this is `warm.push_cadence`'s THE REPO SET derivation: the set of repos
    a server has actually served is built from exactly this call, never
    from a disk scan. Resolved here (before `dispatch`, zero spawns, an
    in-process read of the already-parsed envelope) rather than inside
    `_run_dispatch`/`_pool_dispatch_worker`, so it is recorded identically
    whether this request runs in-process or in a pool worker process --
    `_pool_dispatch_worker` runs in a SEPARATE process and cannot write
    back into this server's own served-repo set. A request naming no
    worktree (a central/`none`-scoped op) records nothing.
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
    caller_payload = msg.pop("_caller", None)
    caller = caller_context.resolve_caller_context(
        caller_payload if isinstance(caller_payload, Mapping) else None
    )

    if client_token is None:
        _write_and_release(_untrusted_caller_response(request_id))
        return

    if version_state.is_skewed(client_token):
        # WHICH AXIS, not just that one fired. `is_skewed` evaluates both and
        # leaves the deciding set behind precisely so this row can name it --
        # axis 1 (token) means a publish stranded this server, axis 2 (source)
        # means something edited engine source in the clone serving the fleet,
        # and an undifferentiated `skew` count sends the next reader at
        # whichever one they already suspected. `getattr` because
        # `version_state` is an injected seam here, and a test double
        # predating `last_skew_axes` must not start failing on an attribute it
        # has no reason to carry.
        axes = getattr(version_state, "last_skew_axes", ())
        record_exit(
            telemetry.EXIT_REASON_SKEW,
            ",".join(axes) if axes else None,
        )
        skew.evict_on_skew(
            respond=_write_and_release,
            close_listener=close_listener,
            drain=drain,
            request_id=request_id,
            server_sha=server_sha,
            client_token=client_token,
        )
        return

    # AFTER skew, BEFORE dispatch. After skew because a stale server's
    # generation is the more fundamental disagreement and evicting is the
    # stronger response; before dispatch because the whole value of this
    # refusal is that it is provably pre-dispatch -- see
    # `_settings_home_refusal`'s own docstring, and `door_core.c ::
    # is_provably_undispatched`, which relies on exactly that placement to let
    # the native door re-run the call cold.
    #
    # The field is POPPED, not merely read, like `_engine_token` and
    # `_caller` above it: `dispatch_message` validates the envelope it is
    # handed, and transport metadata must never reach an op's params.
    #
    # Costs nothing when nothing is claimed. `request_claim` is a dict lookup;
    # this server's own `settings_home()` is resolved only once a claim is
    # actually present, so the ordinary invocation -- no override anywhere,
    # which is every user-path call -- pays one `dict.pop` and no resolution.
    claimed_home = settings_home_claim.request_claim(msg)
    msg.pop(settings_home_claim.SETTINGS_HOME_FIELD, None)
    if claimed_home is not None:
        from coordinator_core._settings_home import settings_home as _resolve_settings_home

        served_home = str(_resolve_settings_home())
        if not settings_home_claim.claims_agree(claimed_home, served_home):
            _write_and_release(
                _settings_home_refusal(request_id, claimed_home, served_home)
            )
            return

    try:
        from coordinator_core.ipc import resolve_request_repo

        origin_repo = resolve_request_repo(msg)
    except Exception:  # noqa: BLE001 -- recording the served repo must never fail a caller
        origin_repo = None
    if origin_repo is not None:
        try:
            record_served_repo(origin_repo)
        except Exception:  # noqa: BLE001 -- see above
            pass

    try:
        response = dispatch(msg, caller=caller)
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
    record_exit: Callable[..., None] = lambda reason, detail=None: None,
    record_served_repo: Callable[[Any], None] = lambda repo_root: None,
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
            record_served_repo=record_served_repo,
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


def _wrap_socket(conn: Any) -> Any:
    """POSIX counterpart of `_wrap_handle`: an accepted unix-socket
    connection as the SAME blocking file-object API `_handle_connection`
    already drives (`readline` / `write` / `flush` / `close`).

    `conn.close()` immediately after `makefile` is the documented CPython
    idiom, not a bug: `socket.makefile` takes its own reference to the
    underlying fd, so the socket stays open until the file object is
    closed too. Dropping the socket object here is what makes
    `_handle_connection`'s single `io.close()` in its `finally` release
    the whole connection -- without it, every served request would leak a
    socket object for the life of a resident server.
    """
    io = conn.makefile("rwb")
    conn.close()
    return io


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
        listen_socket: Any = None,
        endpoint_path: Optional[Path] = None,
    ):
        self.name = name
        self.sid = sid
        # POSIX only, both None on Windows: the listening unix socket this
        # server owns and the path it is bound to, held so `_ctx_shutdown`
        # can remove that path -- ownership-checked, see there. Every
        # pre-existing construction omits both and keeps its exact prior
        # behaviour, which is what `test_server_loop.py`'s direct
        # `_ServerContext(...)` builds depend on.
        self.listen_socket = listen_socket
        self.endpoint_path = endpoint_path
        self._endpoint_identity = (
            election.socket_identity(endpoint_path) if endpoint_path is not None else None
        )
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
        # `warm.push_cadence`'s THE REPO SET: every distinct worktree root
        # this server has actually served (`_serve_line`'s
        # `record_served_repo`), never a disk scan. A `dict` preserves
        # first-served order (Python 3.7+) purely for deterministic test
        # output -- iteration order carries no other meaning here, every
        # sweep visits the whole set.
        self._served_repos_lock = threading.Lock()
        self._served_repos: "dict[Path, None]" = {}
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
        """STOPS ACCEPTING; DOES NOT CLOSE THE ENDPOINT. Flips one
        in-process flag, which the accept-loop guards read to stop
        replenishing pipe instances / accepting new connections. The
        OS-level close and unlink are `_ctx_shutdown`'s step 3, AFTER the
        drain, up to `lifecycle._drain_ceiling_secs` (35s).

        The name is why this docstring exists. Callers -- `skew.
        evict_on_skew`, `lifecycle.begin_shutdown` -- run it first
        precisely to release the endpoint early, and both said so in their
        own docstrings until 2026-08-26. It does not: while the drain runs,
        the endpoint stays bound, a caller is accepted and dropped with
        zero bytes (non-spawning per `warm/client.py`'s table), and a
        SAME-TOKEN successor cannot bind at all. Different-token successors
        are unaffected -- they bind a different endpoint. See
        `docs/research/2026-08-26-repo-warm-succession.md` § 2; moving the
        release here is advisory item 3 and is a plan, not an edit.
        """
        with self._listening_lock:
            self._listening = False

    def _is_listening(self) -> bool:
        with self._listening_lock:
            return self._listening

    def record_invocation(self, warm: bool) -> None:
        self.telemetry.record_invocation(warm=warm)

    def record_served_repo(self, repo_root: Any) -> None:
        """Record one more envelope-carried worktree root into this
        server's served-repo set -- `warm.push_cadence.on_idle_tick`'s
        `served_repos` callable (bound via `self.served_repos`) reads this
        set live on every sweep tick.
        """
        with self._served_repos_lock:
            self._served_repos[Path(repo_root)] = None

    def served_repos(self) -> list:
        """A snapshot list of every worktree root recorded so far -- a
        live read at call time, never a value captured once at boot, per
        `warm.push_cadence.ServedReposFn`'s own contract.
        """
        with self._served_repos_lock:
            return list(self._served_repos.keys())

    def record_exit(self, reason: str, detail: Optional[str] = None) -> None:
        self.telemetry.record_exit(reason, detail)

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

    def _pool_dispatch(self, msg: dict, *, caller: Optional[CallerContext] = None) -> dict:
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
            future = self._ensure_dispatch_pool().submit(_pool_dispatch_worker, msg, caller)
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
            # `isolated=False`, explicitly: this fallback runs the op IN
            # THIS process, on this connection's own accept-thread -- the
            # exact threaded, unisolated shape the spike measured 8/8
            # contaminated (C3's own body) -- so it must take no `os.environ`
            # borrow, only the thread-safe ContextVar bind.
            return _run_dispatch(msg, caller=caller, isolated=False)

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
        # POSIX only. `os._exit(0)` closes the listening socket at the OS
        # level either way, but it does NOT remove the socket FILE, and a
        # file left behind is the corpse every future `bind()` fails
        # against (`warm.election`'s module docstring). Removing it here
        # is what keeps the successor's election a plain bind rather than
        # a probe-and-reclaim.
        #
        # Ownership-checked for exactly the reason the breadcrumb unlink
        # above is: a superseded generation reaching this point may be
        # exiting while a SUCCESSOR already owns this path, and an
        # unconditional unlink would delete the live successor's endpoint
        # -- leaving a healthy server bound to an unlinked inode that no
        # client can reach. `unlink_if_owned` compares (st_dev, st_ino)
        # against what this server bound.
        if self.listen_socket is not None:
            try:
                self.listen_socket.close()
            except Exception:  # noqa: BLE001 -- never raises, per this method's contract
                pass
        if self.endpoint_path is not None:
            election.unlink_if_owned(self.endpoint_path, self._endpoint_identity)
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
        deliberately IN SCOPE, not a bug, but it never reaches the
        mismatch comparison above: `skew.compute_client_token` raises
        `UnstampedEngineRootError` for an unstamped root (no
        `.git/HEAD`-based fallback -- that path was removed), which this
        method's bare `except` catches and turns into `False` per the
        "Never raises" note above. So an unstamped server's `token_stale`
        verdict is always False, and its SUPERSEDED arm can never fire --
        it can only retire via ordinary idle demotion, never early
        retirement on a rotated token.
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
        # `warm.push_cadence`'s cadence counter, on this SAME tick -- see
        # that module's HOST section. Placed AFTER `demote_if_idle` so a
        # tick that actually demotes never runs a sweep of its own here
        # (the process has already `os._exit`ed inside `begin_shutdown` by
        # the time control would reach this line): the idle-tick sweep and
        # the mandatory exit-path sweep (`_final_sweep`, wired via
        # `lifecycle.set_final_sweep_hook`) are deliberately two disjoint
        # code paths, never double-invoked for the same tick.
        push_cadence.on_idle_tick(served_repos=self.served_repos)

    def _final_sweep(self) -> None:
        """The mandatory final sweep (`warm.push_cadence` module docstring,
        `warm.lifecycle._run_tail`'s own docstring) -- registered once, at
        boot, as `lifecycle`'s final-sweep hook, so it runs on EVERY exit
        path through `_run_tail` (idle demotion, skew eviction, operator
        request, degraded self-stop) regardless of which entry point fired.
        Bounded by `push_cadence.EXIT_SWEEP_CEILING_SECS`, tighter than the
        idle-tick sweep's own ceiling -- see that constant's docstring for
        why an unbounded exit sweep is worse than an unbounded idle-tick one.
        """
        push_cadence.sweep_repos(
            self.served_repos(), total_ceiling_secs=push_cadence.EXIT_SWEEP_CEILING_SECS
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
        lifecycle.set_final_sweep_hook(self._final_sweep)
        self._start_worker_pool()
        self._start_pending_listener_pool(first_handle)
        threading.Thread(target=self._idle_watchdog_loop, daemon=True).start()
        self._stopped.wait()

    def serve_forever_unix(self, listen_socket: Any) -> None:
        """POSIX counterpart of `serve_forever`. Same three layers, same
        order, same blocking tail -- only the accept layer differs.

        THE ACCEPT LAYER IS SIMPLER ON POSIX, AND THAT IS NOT AN OVERSIGHT.
        Windows needs `PENDING_LISTENER_POOL_SIZE` pre-created pipe
        INSTANCES, each with a `ConnectNamedPipe` posted on it and each
        replenishing itself one-for-one, because a named pipe with no free
        instance rejects a connecting client outright. A listening unix
        socket has no such shape: one socket accepts every connection, and
        the kernel's `listen()` backlog (`election.UNIX_LISTEN_BACKLOG`) is
        what holds arrivals between accepts -- so there is nothing to
        pre-create and nothing to replenish. What is preserved is the
        property the Windows pool exists FOR: `ACCEPTOR_POOL_SIZE`
        independent threads are simultaneously able to take a connection
        off the kernel and enqueue it, so no single slow enqueue serializes
        arrivals.

        Everything downstream of the enqueue is literally the same code on
        both platforms -- one `queue.Queue`, one `WORKER_POOL_SIZE` pool of
        `_worker_loop` threads, one `InFlightCounter`, one idle watchdog --
        which is the point: the accept-and-queue split the module
        docstring's DRAIN SEMANTICS names is transport-independent, and
        this method changes only what sits on its far side.
        """
        lifecycle.set_final_sweep_hook(self._final_sweep)
        self._start_worker_pool()
        self._start_acceptor_pool(listen_socket)
        threading.Thread(target=self._idle_watchdog_loop, daemon=True).start()
        self._stopped.wait()

    def _start_acceptor_pool(
        self, listen_socket: Any, *, pool_size: int = ACCEPTOR_POOL_SIZE
    ) -> None:
        """Start `pool_size` acceptor threads on one listening unix socket.
        Split out of `serve_forever_unix` for the same isolated-exercise
        reason `_start_pending_listener_pool` is split out of
        `serve_forever`."""
        for _ in range(pool_size):
            threading.Thread(
                target=self._acceptor_loop, args=(listen_socket,), daemon=True
            ).start()

    def _acceptor_loop(self, listen_socket: Any) -> None:
        """One acceptor thread's whole life: accept, wrap, enqueue, repeat.

        This thread's job ends at enqueue, exactly as
        `_accept_and_replenish`'s does -- dispatch happens on a bounded
        `_worker_loop` worker, never here, so acceptance can never spawn an
        unbounded number of dispatching threads (AC7).

        A connection accepted after `close_listener` is closed WITHOUT being
        served, which is the same outcome the Windows chain produces for a
        pipe instance connected after the listener closed: the client reads
        EOF and goes cold rather than being answered by a generation that is
        already draining.

        An `OSError` from `accept()` ends this thread rather than looping.
        The one way it happens is the listening socket being closed --
        `_ctx_shutdown`, immediately before `os._exit(0)` -- so retrying
        would spin a doomed thread against a dead fd for the microseconds
        the process has left. A wrap-or-enqueue failure for ONE connection
        is not that: it drops that connection and keeps accepting, on the
        same reasoning `_worker_loop`'s own guard is load-bearing for --
        an acceptor that dies on one bad connection is a permanent capacity
        loss, not a single lost response.
        """
        while True:
            try:
                conn, _ = listen_socket.accept()
            except OSError:
                return

            if not self._is_listening():
                try:
                    conn.close()
                except OSError:
                    pass
                return

            try:
                io = _wrap_socket(conn)
            except Exception as exc:  # noqa: BLE001 -- see docstring
                print(
                    f"[warm-server] failed to wrap an accepted connection; "
                    f"dropping it and continuing to accept: {exc!r}",
                    file=sys.stderr,
                )
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            self._enqueue_connection(io)

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

        THE `except Exception` BELOW IS LOAD-BEARING, NOT DEFENSIVE
        BOILERPLATE. `_handle_connection` catches `OSError` around its own
        `io.readline()`, and `_serve_line` catches `Exception` around its
        own `dispatch(...)` call -- but neither wraps `_write_and_release`'s
        `write(_encode(response))`, which is `io.write(data)` under the
        module docstring's TRANSPORT MODEL. A client that hit its own
        `READ_DEADLINE_SECS` (2s) and went cold already closed its pipe
        handle -- `warm.client`'s own comment calls this "the common case on
        this box, not a corner" under the stated load norm -- so by the time
        a busy worker finally reaches `_write_flushed`, that write raises an
        unhandled `OSError` (a broken/closed pipe). Before this guard, that
        exception propagated straight out of this `while True:` loop and
        killed the thread -- silently and permanently, since nothing
        restarts a dead worker. Live evidence, this server (pid unchanged
        across the observation): `WORKER_POOL_SIZE` is 30, but a
        `py-spy dump` found only 6 of the original 30 numbered worker
        threads still alive, the rest having died the same way one at a
        time. Each death shrinks real dispatch concurrency further below
        the pool's own bound, which lengthens the shared queue's wait under
        the SAME load, which makes MORE clients hit their own 2s deadline
        and abandon -- a self-reinforcing die-off, not a one-time fluke,
        and the direct cause of the intermittent
        `warm dispatch unavailable` (-32603) rc=1 this guard closes.
        Logged and continued, never re-raised: the module's own "NEVER FAIL
        A CALLER" contract already accepts that one connection can be lost
        to a broken pipe (the client is gone; there is no caller left to
        answer) -- what this closes is the OTHER cost, this worker thread's
        own survival for the NEXT connection in the queue.
        """
        while True:
            io = self._queue.get()
            try:
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
                    record_served_repo=self.record_served_repo,
                    already_entered=True,
                )
            except Exception as exc:  # noqa: BLE001 -- see docstring: a dead worker is a permanent capacity loss, not a single lost response
                print(
                    f"[warm-server] worker thread survived an unhandled exception "
                    f"from _handle_connection (likely a write to an abandoned "
                    f"connection); continuing to drain the queue: {exc!r}",
                    file=sys.stderr,
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

    1. Resolve this engine clone's endpoint -- a pipe name on Windows, a
       unix socket path on POSIX -- using the SAME primary skew token the
       client computes (`skew.compute_client_token`) as the endpoint's
       generation stamp; a successor bound to a new commit computes a
       different token, hence a different endpoint, and binds immediately
       without contesting the predecessor's still-draining one
       (election.py's own docstring).
    2. Elect it (`election.elect` / `election.elect_unix_socket`).
       `ElectionLost` means another process already won this exact
       generation's endpoint -- not an error, just nothing for this
       process to do; exits 0.
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

    STEP 0, ADDED 2026-08-21 (state/subagent-share silent-failure audit):
    everything from the repo-root resolution below through `serve_forever`'s
    own setup is wrapped in `_run_guarded` and reports any crash via
    `ops.ceremony.detached_spawn.record_child_failure` before re-raising.
    Before this, a crash here (a bad token, an `election.elect` failure
    other than `ElectionLost`, a `_ServerContext` construction error) wrote
    an uncaught traceback to `sys.stderr` -- which `detached_spawn.
    spawn_detached` opens as `subprocess.DEVNULL` for every detached child,
    this one included -- and reached no log, no telemetry, nothing: the
    parent's own `spawn_detached` call had already returned `True` (Popen
    itself didn't raise) and never reads this process's exit code. See that
    module's own "CHILD FAILED" contract, which this was the one spawn site
    never wired into it. Deliberately NOT swallowed: the re-raise below
    propagates straight out of `sys.exit(main())` at module scope -- the
    interpreter's own unhandled-exception path, exit code 1, exactly as it
    would have before this guard existed -- per PM ruling, this must die
    audibly, not survive as a degraded no-op.
    """
    try:
        return _run_guarded()
    except Exception as exc:  # noqa: BLE001 -- log, then re-raise; see docstring's STEP 0
        try:
            from coordinator_core.ops.ceremony.detached_spawn import record_child_failure

            record_child_failure(str(_engine_clone_root()), __file__, exc=exc)
        except Exception:  # noqa: BLE001 -- the failure record must not hide the original crash
            pass
        raise


class _Elected(NamedTuple):
    """What a boot-election arm hands back.

    EXACTLY ONE OF `first_handle` / `listen_socket` IS EVER SET, and which
    one names the transport this process won. `_run_guarded` dispatches on
    THAT rather than re-reading the platform, so the serve call is narrowed
    by the same value it consumes -- a static reader and the interpreter
    agree, and neither `serve_forever` nor `serve_forever_unix` can be
    reached with the other platform's `None`. A result with neither set is
    a construction bug and raises rather than serving nothing.

    A NamedTuple rather than a bare 5-tuple: the positional unpack read the
    same for both arms while meaning opposite things in two slots, which is
    exactly the shape a later edit transposes silently.
    """

    endpoint: str
    identity: str
    first_handle: Optional[int]
    listen_socket: Any
    endpoint_path: Optional[Path]


def _elect_windows_pipe(repo_root: Path, token: str) -> _Elected:
    """Windows arm of the boot election: resolve the SID, build the pipe
    name, take the first instance. `first_handle` always an int here, the
    two POSIX slots always None.

    Lifted VERBATIM out of `_run_guarded` when the POSIX arm landed
    (2026-08-21) -- same calls, same order, same messages. The extraction
    exists so the shared tail below (version state, breadcrumb, route
    declaration, preload, context, serve) is written once instead of twice
    and cannot drift between platforms.
    """
    sid = election.current_user_sid()
    name = election.pipe_name(token, engine_clone=repo_root, user_sid=sid)
    first_handle = election.elect(name, user_sid=sid)
    return _Elected(name, sid, first_handle, None, None)


def _elect_unix_socket_endpoint(repo_root: Path, token: str) -> _Elected:
    """POSIX arm of the boot election. Same return shape as
    `_elect_windows_pipe`, with the slots swapped: `first_handle` always
    None here, a listening socket and the path it is bound to instead.

    `identity` carries `election.current_user_id()` -- the uid, the identity
    analog of the SID. It reaches `_ServerContext.sid`, where its ONLY
    Windows consumer (`_create_pipe_instance`'s ACL) is on a code path this
    platform never takes; it is carried anyway so the context's identity
    field is populated rather than a lie or a `None` on POSIX.
    """
    uid = election.current_user_id()
    path = election.socket_path(token, engine_clone=repo_root)
    listen_socket = election.elect_unix_socket(path)
    return _Elected(str(path), uid, None, listen_socket, path)


def _run_guarded() -> int:
    """`main()`'s actual boot sequence -- factored out so `main()` can wrap the
    whole thing in one guard (see `main`'s STEP 0) without the guard itself
    needing to duplicate the platform branch or the crash-reporting glue.

    ONE SEQUENCE, TWO TRANSPORTS. Only the election step differs by
    platform (`_elect_windows_pipe` / `_elect_unix_socket_endpoint`) --
    every later step, including which generation token is computed, what
    the breadcrumb records, when the route is declared, and the whole
    accept/queue/worker shape, is the same code on both. A POSIX server is
    therefore not a second server that happens to resemble this one; it is
    this one with a different endpoint under it.
    """
    repo_root = _engine_clone_root()
    # The spawner's clock at the instant it launched this process, if this
    # process was launched by a route that stamps it (`warm.client._spawn_once`
    # does; a SessionStart warm_start does not). None means "unmeasurable from
    # here", and `_record_own_boot` writes nothing rather than inventing a
    # start -- an invented t0 would be indistinguishable from a measured one in
    # the file that exists to settle how long boot takes.
    spawn_epoch = _spawn_epoch_from_env()
    token = skew.compute_client_token(repo_root)
    on_windows = sys.platform == "win32"

    try:
        elected = (
            _elect_windows_pipe(repo_root, token)
            if on_windows
            else _elect_unix_socket_endpoint(repo_root, token)
        )
    except election.ElectionLost as lost:
        # `lost.endpoint` is the contested name/path, carried on the
        # exception because the election arm raises before it can return
        # one. The wording stays platform-specific: on Windows this is the
        # exact line the operator's logs have carried since C30.
        endpoint_word = "pipe" if on_windows else "socket"
        print(
            f"[warm-server] election lost for {lost.endpoint!r}; another server already "
            f"owns this generation's {endpoint_word}, exiting",
            file=sys.stderr,
        )
        # THE ONE EXIT THAT REACHED NO FILE. The print above is the whole
        # historical record of a failed succession, and `spawn_detached`
        # opens this process's stderr as DEVNULL, so it reaches nothing --
        # every exit-reason census in the 2026-08-26 succession
        # investigation is over surviving rows only, censored upward
        # (docs/research/2026-08-26-repo-warm-succession.md § 5.1). A
        # same-token successor locked out by a draining predecessor lands
        # here, so this row is the instrument any fix to that ordering is
        # verified against. Written before, not instead of, the exit-0:
        # losing is still not an error.
        telemetry.record_election_lost(
            endpoint=lost.endpoint,
            token=token,
            pid=os.getpid(),
            lost_secs=(time.time() - spawn_epoch) if spawn_epoch is not None else None,
            engine_root=repo_root,
        )
        return 0

    # ELECTION IS THE FIRST INSTANT A CLIENT CAN CONNECT: the endpoint exists
    # from here on, so this is where "spawn -> connectable" stops running.
    listener_at = time.time()

    version_state = skew.ServerVersionState(repo_root)

    try:
        breadcrumb.write_breadcrumb(
            pipe=elected.endpoint,
            pid=os.getpid(),
            stable_pid_start_epoch=_self_stable_pid_start_epoch() or 0,
            engine_sha=version_state.server_sha,
            engine_root=repo_root,
            transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX,
        )
    except Exception as exc:  # noqa: BLE001 -- a HINT writer failing must not stop the server
        print(f"[warm-server] failed to write breadcrumb: {exc!r}", file=sys.stderr)

    _declare_execution_route()

    _suppress_pool_worker_consoles()

    _preload_op_registry()

    # ...and this is the first instant a connection gets a PROMPT answer:
    # `_preload_op_registry` above is the ~703ms of imports that would
    # otherwise land on whichever caller arrived first. Both instants are
    # recorded because a client that reaches the first still waits for the
    # second.
    #
    # RECORDED BEFORE `ensure_listener`, MOVED 2026-08-26. This call used to sit
    # below that block, so every `ready_secs` on disk silently included up to
    # `supervisor.HEALTH_CHECK_TIMEOUT_SECS` of somebody else's health probe --
    # a cost that has nothing to do with whether THIS server can answer, in the
    # one row that exists to say when it can. The succession investigation
    # reasoned from `ready_secs` throughout and never saw it, because its
    # sandbox had no stale discovery record to make the probe wait.
    _record_own_boot(spawn_epoch, listener_at, repo_root)

    # C2 (docs/plans/2026-08-25-the-http-listener-gets-something-keeping-it-up.md):
    # this pipe server is the one resident, per-machine, elected process the box
    # runs, so its own boot -- past its OWN election, never before -- is what gives
    # the http listener a supervisor that is itself supervised. Lazy-imported: a
    # module-level `from coordinator_core.warm import supervisor` would be
    # circular, since `supervisor.py` itself imports `InFlightCounter`/`_serve_line`
    # from this module.
    #
    # ON ITS OWN THREAD, MOVED OFF THE CRITICAL PATH 2026-08-26. `ensure_listener`
    # is documented never to wait; it waits up to `HEALTH_CHECK_TIMEOUT_SECS`
    # (2.0s) inside `check_health`, a synchronous `urlopen`, whenever a discovery
    # record names a live pid whose listener has hung (see that function's own
    # corrected docstring, and `docs/research/2026-08-26-repo-warm-succession.md`
    # § 4). Paid here, it lands on the successor's time-to-answerable during
    # exactly the window a caller is already waiting out a predecessor's drain.
    #
    # NOTHING DEPENDS ON THE ORDERING, which is what makes this safe rather than
    # merely faster: the return value is ignored by contract, and the OTHER
    # production call site (`warm/entry_seam.py :: _trigger_listener_boot`, C3)
    # exists precisely to cover the case this one does not -- neither process
    # running. The two are redundant coverage of one goal, not a chain, so the
    # worst case here is that the http listener starts a few hundred ms later
    # and the next hook fire nudges it anyway.
    #
    # The try/except stays despite the thread: a daemon thread's uncaught
    # exception would print a traceback to a stderr `spawn_detached` opens as
    # DEVNULL, and this boot sequence must not take "documented never to raise"
    # on faith -- the same reason the breadcrumb write above is wrapped.
    def _ensure_http_listener() -> None:
        try:
            from coordinator_core.warm import supervisor

            supervisor.ensure_listener(repo_root)
        except Exception as exc:  # noqa: BLE001 -- fail-open by construction
            print(f"[warm-server] http listener ensure_listener() failed: {exc!r}", file=sys.stderr)

    threading.Thread(
        target=_ensure_http_listener, daemon=True, name="warm-http-ensure-listener"
    ).start()

    ctx = _ServerContext(
        name=elected.endpoint,
        sid=elected.identity,
        version_state=version_state,
        engine_root=repo_root,
        boot_token=token,
        listen_socket=elected.listen_socket,
        endpoint_path=elected.endpoint_path,
    )

    # Dispatch on WHICH ENDPOINT WAS WON, not on the platform read again.
    # The value that decides is the value that gets passed, so the
    # not-None check IS the narrowing -- there is no path on which
    # `serve_forever` sees a POSIX `None` or `serve_forever_unix` a
    # Windows one. An election that returned neither served nothing and
    # exited 0 silently before this branch existed; it now dies audibly,
    # which is what `main`'s STEP 0 guard is for.
    if elected.first_handle is not None:
        ctx.serve_forever(elected.first_handle)
    elif elected.listen_socket is not None:
        ctx.serve_forever_unix(elected.listen_socket)
    else:
        raise election.ElectionError(
            f"election returned no endpoint to serve for {elected.endpoint!r}"
        )
    return 0


def _spawn_epoch_from_env() -> "float | None":
    """This process's spawn instant as stamped by its spawner, or None when
    unstamped or unparseable. Never raises: a malformed value means the
    measurement is unavailable, which is exactly what None already says."""
    raw = os.environ.get(telemetry.SPAWN_EPOCH_ENV)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _record_own_boot(
    spawn_epoch: "float | None", listener_at: float, repo_root: Path
) -> None:
    """Write this server's own boot durations, if its spawn was stamped.

    THE NUMBER NO CLIENT CAN OBSERVE. A client sees only that a server was
    absent at the moments it happened to ask, so every client-derived estimate
    of boot is censored by its own call pattern -- which is why
    `client-cold.jsonl` supports two readings of this box's outages that
    disagree by 9x on the median, and why the server-succession gaps in
    `telemetry.jsonl` are bounded the other way (the next server does not start
    until a caller arrives to trigger a spawn, so those gaps measure caller
    absence as much as boot). This process is the only one that can time the
    interval with no caller in it at all.

    Best-effort and silent on failure, matching the breadcrumb write it sits
    beside: an instrument must never be why a server fails to boot."""
    if spawn_epoch is None:
        return
    try:
        now = time.time()
        telemetry.record_server_boot(
            listener_secs=listener_at - spawn_epoch,
            ready_secs=now - spawn_epoch,
            pid=os.getpid(),
            engine_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        print(f"[warm-server] failed to record boot timing: {exc!r}", file=sys.stderr)


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
    generations are not rare. `skew.compute_client_token` keys on the
    published engine's build stamp (`coordinator_core/_engine_stamp`), so
    the token rotates once per publish round, not per commit -- a live
    (unstamped) working tree instead raises `UnstampedEngineRootError`
    from `compute_client_token`, which `_token_is_stale`'s bare except
    turns into "not stale" (see that method's docstring). Either way each
    new generation re-presents that 703 ms to its first caller. A server
    that is evicted early can spend most of its life having served
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

    EAGER, DELIBERATELY (2026-08-22): calls `coordinator_core.ops._eager_import_all()`
    directly rather than a bare `import coordinator_core.ops`. Under the lazy-only
    package (`ops/__init__.py` no longer eager-imports anything at package-init
    time), a bare import registers NOTHING and raises nothing — the except branch
    below never fires, nothing is printed, and this preload silently becomes a
    no-op while the server reports healthy. The warm server is the ONE caller in
    this repo that legitimately wants a full eager registry: a long-lived process
    serving arbitrary ops, the precise case the lazy channel was carved AROUND
    rather than for (see `op_census/spawn_bearing_ops.py`'s census enumerators for
    the same pattern). Do not revert this to a bare import.
    """
    try:
        import coordinator_core.ops as _ops_pkg

        _ops_pkg._eager_import_all()  # noqa: SLF001 -- the one legitimate eager caller, see docstring above
        from coordinator_core.ipc import dispatch_message  # noqa: F401
    except Exception as exc:  # noqa: BLE001 -- a slow first call beats no server
        print(f"[warm-server] op-registry preload failed: {exc!r}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
