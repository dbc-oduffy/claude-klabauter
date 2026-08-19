"""
coordinator_core.warm.entry_seam — the one per-request state seam all four
engine entry paths converge on.

Purpose: under a warm, long-lived process, per-request state must be
explicitly opened and reset for EACH dispatch rather than relying on the
"only one dispatch ever runs in this process" ambient inheritance a
per-invocation process could get away with. `coordinator_core.ipc`'s own
dispatch core (`_dispatch_message_impl`) already does this explicitly (C11:
`session.declared_writes`'s `ContextVar` is bound via `.set()`'s Token and
unwound via `.reset()` in a `finally`, rather than a bare rebind). This
module factors that same explicit-scope shape out to a seam the OTHER entry
paths can converge on, instead of each growing its own copy.

Four engine entry paths, verified at HEAD (2026-08-15/16):
  1. `ipc.dispatch_message` -> `ipc._dispatch_message_impl` — a telemetry
     wrapper around the dispatch core. Already carries per-request state
     explicitly (C11). Converged.
  2. `cli_entry.run_op_main` — reaches op modules by PLAIN IMPORT; touches
     neither the registry, `ipc._timeout_for`, nor `resolve_op_repo_key`
     (it runs an op's CLI `main(argv)`, not its JSON-RPC handler). Its own
     declared-writes collection now opens through THIS module's
     `per_request_state`, converging it onto the same explicit-scope
     mechanism `_dispatch_message_impl` uses, rather than calling
     `session.declared_writes.collecting()` directly.
  3. `get_op_handler` re-entry — production call sites across `coordinator_
     core.ops.*` (and `baton_assemble.apply`) that resolve a handler by key
     and invoke it directly, bypassing dispatch entirely: no per-request
     declared-writes scope, no timeout, no repo-key resolution. This is the
     largest un-instrumented surface and the reason this module exists as a
     standalone convergence point rather than as a private helper inside
     `cli_entry.py` — a call site in any of those modules can adopt
     `reentrant_dispatch()` below without importing `cli_entry` (which is a
     CLI-trampoline concern, not theirs) or hand-rolling `collecting()`
     itself. Migration is per-site and tracked as a residual of this chunk;
     this module only authors the primitive.
  4. `ipc.dispatch_from_hook` — wraps `asyncio.run(dispatch_message(...))`,
     so it inherits path 1's convergence transitively. Converged.

Negative-spec (RAG-bait):
    This module does not decide WHICH declared-write list an op sees, does
    not resolve session identity from an env/environment-derived source (it
    only BINDS an identity a caller already resolved and handed in — see
    `per_request_state`'s `session_id` parameter and `session.core.
    session_identity_override`), and does not record anything to disk. It
    delegates collection to `session.declared_writes.collecting()` (already
    `ContextVar` Token/reset-scoped, so nesting is safe) and recording
    remains each caller's own job via `ipc._record_self_reported_touches` —
    exactly the split `cli_entry.recording_declared_writes` already had.
    This module introduces no second declare/record dialect; it is a
    convergence point for the SCOPING half, plus (as of C-warm-identity)
    the per-request IDENTITY-BINDING half — never identity RESOLUTION,
    which stays `session.core`'s job alone.

    `reentrant_dispatch` deliberately does not add `asyncio.wait_for` timeout
    wrapping or a JSON-RPC envelope. Every audited path-3 call site invokes
    its resolved handler synchronously without awaiting it (e.g.
    `ops/ceremony/wsc_tail.py::_derive_trailers`'s
    `handler({"session_id": sid, "nature": nature}, common_dir)`), so
    handlers reached this way are sync in practice today; adding async
    dispatch machinery here would be inventing a capability no live call
    site uses. A caller needing the full JSON-RPC contract (timeout, error
    envelope, lazy-import fallback) should call `ipc.dispatch_message`
    instead — this seam is for the narrower, already-in-process re-entry
    shape path 3 uses.

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-
cold-start.md task C13.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from typing import Any, Iterator, List, Optional

from coordinator_core.session.core import session_identity_override
from coordinator_core.session.declared_writes import collecting

__all__ = [
    "per_request_state",
    "reentrant_dispatch",
    "emit_diagnostic",
    "collecting_diagnostics",
]


# ---------------------------------------------------------------------------
# Per-request DIAGNOSTIC axis (2026-08-19).
#
# WHY THIS EXISTS. A fleet setup error returns the FROZEN exit_code:1 envelope,
# which carries no reason field by contract (§2.1), so `ops/fleet/_common.py
# :: _setup_error`'s write to the op process's stderr is the ONLY diagnostic
# channel such a refusal has — that helper's own docstring says as much, and
# `ops/fleet/tests/test_setup_error_stderr_channel.py` pins both halves.
#
# That guarantee holds for a COLD spawn, where the op process IS the caller's
# child and its stderr is the caller's pipe. Under the warm engine it does not:
# the op runs inside the SERVER process, so the reason lands on the server's
# stderr and the caller sees `refused (exit_code=1, failed=0)` with the reason
# nowhere. Same op, same refusal, same caller code — served warm it is mute.
# Reported by doe-claude-em (cross-repo/inbox/2026-08-19-doe-claude-em-warm-
# engine-seam-async-declined.md), who diagnosed it as the CLI forwarder failing
# to read stderr on the rc==0 path; the forwarder is correct and does read it
# (`coordinator/bin/lib/cc_invoke.py :: route_mutation`'s `_stderr_sink`) —
# there was simply nothing on that stream to read.
#
# The fix is an explicit per-request sink rather than capturing `sys.stderr`:
# connections are served on their own OS threads, so redirecting the process-
# global stream would interleave one caller's diagnostics into another's frame.
# A ContextVar is bound per dispatch and is invisible to every other request.
#
# Cold path is untouched: nothing binds the sink, `emit_diagnostic` is a no-op,
# and `_setup_error` keeps writing to stderr exactly as before.
# ---------------------------------------------------------------------------
_DIAGNOSTICS: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    "coordinator_core_op_diagnostics", default=None
)


def emit_diagnostic(text: str) -> None:
    """Record one diagnostic line for the request currently in scope.

    A no-op unless a caller has opened `collecting_diagnostics()` — which only
    the warm server does. Producers call this IN ADDITION to their existing
    stderr write, never instead of it: the stderr write is what a cold spawn's
    caller reads, and this is what a warm caller reads. Neither replaces the
    other, and a producer that emits only here would go silent cold.
    """
    sink = _DIAGNOSTICS.get()
    if sink is not None:
        sink.append(text)


@contextlib.contextmanager
def collecting_diagnostics(into: Optional[List[str]] = None) -> Iterator[List[str]]:
    """Bind a diagnostic sink for the duration of the block, Token/reset-scoped.

    Same discipline as `collecting()`: bound via `.set()`'s Token and unwound in
    a `finally`, so nesting is safe and a request cannot inherit a stale sink.
    """
    sink: List[str] = [] if into is None else into
    token = _DIAGNOSTICS.set(sink)
    try:
        yield sink
    finally:
        _DIAGNOSTICS.reset(token)


@contextlib.contextmanager
def per_request_state(
    into: Optional[List[str]] = None,
    *,
    session_id: Optional[str] = None,
    diagnostics: Optional[List[str]] = None,
) -> Iterator[List[str]]:
    """Open one request's worth of explicit, Token/reset-scoped state.

    Two independent axes, both Token/reset-scoped and both unwound in a
    `finally` regardless of nesting order: `session.declared_writes.
    collecting()` (the pre-existing per-request declared-writes list) and,
    now, `session.core.session_identity_override()` (C-warm-identity: the
    CALLER's resolved session id, when the request carried one — see that
    context manager's own docstring for the full defect this closes).
    Named and kept as its own seam (rather than callers importing either
    primitive directly) so a future per-request concern is a seam to grow
    HERE, once, instead of a parallel context manager invented at each
    entry path.

    `into`, when given, is the list `declare_write()` calls append to
    (matching `collecting()`'s own signature) — callers that already hold a
    list object to hand to a recorder (e.g. `cli_entry.recording_declared_
    writes`) pass it through unchanged rather than this seam allocating a
    second one.

    `session_id`, when given, is the caller's session id to bind for the
    duration of the block — absent/`None`/non-UUID-shaped is a no-op
    (`session_identity_override`'s own fail-safe gate), so every existing
    caller of this function (none of which pass `session_id` today) is
    byte-for-byte unaffected.

    `diagnostics`, when given, is the list `emit_diagnostic()` calls append to
    for the duration of the block — the third axis (see the DIAGNOSTIC-axis
    note above). Omitted/`None` binds no sink, so `emit_diagnostic` stays a
    no-op and a caller that does not ask for diagnostics is unaffected.
    """
    with session_identity_override(session_id):
        with collecting(into) as declared:
            if diagnostics is None:
                yield declared
            else:
                with collecting_diagnostics(diagnostics):
                    yield declared


def reentrant_dispatch(
    op_name: str,
    params: dict,
    *,
    repo_root: Optional[Any] = None,
) -> Any:
    """Invoke a registered op handler in-process, carrying explicit
    per-request state, for a `get_op_handler` re-entry call site (path 3).

    Convergence point for the shape every audited path-3 call site shares:

        handler = get_op_handler(name)
        if handler is None:
            ...
        result = handler(params, repo_root)

    Swapping that for `reentrant_dispatch(name, params, repo_root=...)` gets
    the same declared-writes isolation paths 1/2/4 already have — a nested
    re-entrant call cannot leak its declarations into, or inherit stale
    declarations from, the outer dispatch that reached it — with no change
    to the caller's own timeout, error handling, or repo-resolution logic
    (none of those are this function's concern; see the module docstring's
    negative-spec).

    Migrating a call site to this primitive is additive: a handler that
    raises still raises to the caller unchanged, and the return value is
    the handler's own return value, verbatim.

    Raises:
        LookupError: `op_name` has no registered handler (mirrors the
            `if handler is None` guard every audited call site already
            writes for itself — surfaced here as an exception instead of a
            caller-specific fallback, since this function does not know
            what a given call site's fallback should be).
        TypeError: `op_name` resolves to an `async def` handler. Every
            audited path-3 call site invokes its handler synchronously
            (module docstring), so an async handler reached through this
            seam would silently return an unawaited coroutine instead of a
            result — a future migration onto this primitive is the whole
            reason this guard exists now, before any call site trips it.
    """
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler(op_name)
    if handler is None:
        raise LookupError(f"reentrant_dispatch: no registered handler for {op_name!r}")
    if asyncio.iscoroutinefunction(handler):
        raise TypeError(
            f"reentrant_dispatch: handler for {op_name!r} is async "
            "(asyncio.iscoroutinefunction) — this seam invokes handlers "
            "synchronously and does not await; a caller needing an async "
            "handler must use ipc.dispatch_message instead"
        )

    with per_request_state():
        return handler(params, repo_root=repo_root)
