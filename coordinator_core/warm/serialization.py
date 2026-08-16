"""coordinator_core.warm.serialization — re-provides DR-215 §3's vacated
single-writer guarantee for concurrent MUTATING dispatch under a warm
engine.

Purpose: DR-215 §3 vacated `requires_single_writer_queue` on the explicit
ground that command-type dispatch is "serial by construction" — one
process, one op, no wire to gate. DR-315 §3.2 re-opens that vacatur: a warm
engine holds one persistent server process serving overlapping clients, so
"serial by construction" stops being true the moment a second client can be
mid-dispatch while a first is still being served. This module is the named
mechanism DR-315 requires in its place — it re-provides the GUARANTEE (no
two MUTATING dispatches race each other's writes), not the OLD retired
mechanism (an HTTP-layer 403 over a resident daemon).

WORKER LIFETIME (settled, not reopened here — see this chunk's dispatch
brief): lock-holding work runs in a killable worker PROCESS drawn from a
PERSISTENT pool started alongside the warm server, never a per-op spawn.
This module does not itself start, own, or shut down that pool — Wave D
builds the server; this module only defines the serialization primitive a
future server composes around whatever `concurrent.futures.Executor` it
already owns. `MutatingSerializer.__init__` takes that executor as a
parameter for exactly this reason.

Because a worker is a separate PROCESS, `contextvars.ContextVar`s
(`coordinator_core.contract.apply_base.session_identity()`) do NOT cross
the process boundary — a worker started fresh via `spawn` (Windows' only
start method) begins with none of the submitting process's ambient
contextvar state. `PooledDispatchRequest` is the explicit, picklable carrier
for whatever per-request state a worker needs to reconstruct that identity
inside its own process before running the handler — never an implicit
os.environ/contextvar read performed inside the worker at call time.

Serialization design: this is NOT a cross-process lock (a named-mutex /
`multiprocessing.Lock` primitive is available but adds real Windows
portability risk this DR's Anti-scope already forecloses electing new
platform machinery for — see PIPEWARDEN's own ctypes-or-nothing framing).
Instead, `MutatingSerializer` gates *submission* through one
`threading.Lock` held in the single warm-server process that is the ONLY
submitter into the pool: a MUTATING dispatch acquires the lock BEFORE
`executor.submit(...)` and does not release it until that dispatch's
result has returned, so a second overlapping MUTATING dispatch's task
cannot begin running in ANY worker until the first one's result is back —
whether the pool has one worker or many. A COMPUTE_ONLY dispatch never
touches this lock and may run fully concurrently with any other dispatch,
MUTATING or not, in whatever free worker the pool has — the guarantee is
scoped to the MUTATING half only, per DR-315 §3.2's restated purpose.

Negative-spec:
    - Does NOT start, own, or shut down a worker pool, a pipe, or any
      transport — Wave D (C13+) builds the warm server; this module is
      pool-agnostic and takes an already-constructed `Executor`.
    - Does NOT resurrect the two-tier token matrix or Invariant 3
      (DR-315 §3.2: both stay vacated).
    - Does NOT use a cross-process lock/mutex — see "Serialization
      design" above for why a submission-gating `threading.Lock` in the
      single submitting process is sufficient and is the mechanism
      actually re-provided.
    - Does NOT read `os.environ` or any `contextvars.ContextVar` inside a
      worker to *discover* identity — identity crosses the process
      boundary only via `PooledDispatchRequest`'s explicit fields.
    - Does NOT guess an op's classification — `dispatch` raises
      `UnknownOpClassification` for any `op_name` absent from
      `coordinator_core.authz.classification.OP_CLASSIFICATION` rather
      than defaulting silently (fail-closed, matching that registry's own
      "new ops default to MUTATING until affirmed" discipline: an op this
      mechanism cannot classify must not be allowed to skip the write
      lock by accident).

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C12
Decision: docs/decisions/DR-315-warm-engine-execution-model.md § 3.2
"""

from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import Executor
from typing import Any, Callable, Optional

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass


class UnknownOpClassification(Exception):
    """Raised by `MutatingSerializer.dispatch` when `op_name` has no entry
    in `OP_CLASSIFICATION`. Fail-closed: an unclassified op is a bug in the
    caller (every registered op must be classified per
    `coordinator_core/authz/classification.py`'s own drift guard), never a
    case this mechanism silently routes as COMPUTE_ONLY."""


@dataclasses.dataclass(frozen=True)
class PooledDispatchRequest:
    """Explicit, picklable per-request state carried INTO a pooled worker
    process. `contextvars.ContextVar`s do not cross a process boundary (see
    module docstring), so every field a worker needs to reproduce the
    ambient identity a same-process dispatch would have had must live here
    as a plain value — never read from `os.environ` or a contextvar inside
    the worker at call time.

    `args`/`kwargs` are the positional/keyword arguments for the handler
    this request will run; `session_id`, when set, is entered via
    `coordinator_core.contract.apply_base.session_identity()` inside the
    worker process, for the duration of the handler call only, mirroring
    the per-context (not process-wide) shape that module's own C6 chunk
    established.
    """

    op_name: str
    session_id: Optional[str] = None
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = dataclasses.field(default_factory=dict)


def run_pooled_request(
    handler: Callable[..., Any], request: PooledDispatchRequest
) -> Any:
    """The pool worker's own entrypoint — MUST be a module-level function
    (not a closure/lambda) so it remains picklable under Windows' `spawn`
    start method. Runs `handler(*request.args, **request.kwargs)` inside
    `coordinator_core.contract.apply_base.session_identity(request.session_id)`
    when a session id is present, scoped to this ONE call only — a fresh
    worker process begins with no ambient contextvar state of its own, so
    this is the sole seam that reconstructs it (module docstring)."""
    if request.session_id:
        from coordinator_core.contract.apply_base import session_identity

        with session_identity(request.session_id):
            return handler(*request.args, **request.kwargs)
    return handler(*request.args, **request.kwargs)


class MutatingSerializer:
    """Re-provides DR-215 §3's vacated single-writer guarantee for
    concurrent MUTATING dispatch (§ module docstring). Wraps a
    caller-owned `concurrent.futures.Executor` (a persistent worker pool,
    per DR-315's settled worker-lifetime ruling — this class does not
    create, own, or shut one down) and gates only the MUTATING half of
    dispatch through one process-local `threading.Lock`.
    """

    def __init__(self, executor: Executor):
        self._executor = executor
        self._write_lock = threading.Lock()

    def classify(self, op_name: str) -> OpClass:
        op_class = OP_CLASSIFICATION.get(op_name)
        if op_class is None:
            raise UnknownOpClassification(
                f"{op_name!r} has no entry in OP_CLASSIFICATION — refusing to "
                "dispatch an unclassified op rather than guessing its write "
                "semantics"
            )
        return op_class

    def dispatch(
        self,
        op_name: str,
        handler: Callable[..., Any],
        request: PooledDispatchRequest,
    ) -> Any:
        """Submits `handler(request)` to the pool and returns its result.

        A MUTATING `op_name` acquires `self._write_lock` BEFORE
        `executor.submit(...)` and holds it until the submitted future's
        result has been retrieved — no second MUTATING dispatch's task can
        begin running in the pool while an earlier one is still in flight,
        regardless of how many workers the pool has. A COMPUTE_ONLY
        `op_name` never touches the lock and is submitted immediately,
        free to run concurrently with anything else already in flight.
        """
        op_class = self.classify(op_name)
        if op_class is OpClass.MUTATING:
            with self._write_lock:
                return self._submit_and_wait(handler, request)
        return self._submit_and_wait(handler, request)

    def _submit_and_wait(
        self, handler: Callable[..., Any], request: PooledDispatchRequest
    ) -> Any:
        """Submit `handler(request)` to the pool and block for its result.
        Shared by both `dispatch()` branches — the lock-scoping difference
        (held vs. not held) stays visible at each call site above; only the
        identical submit/wait pair is factored out."""
        future = self._executor.submit(run_pooled_request, handler, request)
        return future.result()
