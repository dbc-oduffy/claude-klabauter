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
  4. `ipc.dispatch_from_hook` and `ipc.dispatch_ops_from_hook` — both wrap
     `asyncio.run(dispatch_message(...))` (the multi-op sibling awaits each op
     sequentially under one loop), so they inherit path 1's convergence
     transitively. Converged.

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
    "WarmGuardOutcome",
    "try_warm_guard_dispatch",
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


from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Warm-first CLIENT PRIMITIVE (C14a, no live caller yet).
#
# WHY THIS EXISTS. C14 (docs/plans/2026-08-22-a-bash-call-stops-costing-a-
# second-and-a-half.md) came back BLOCKED on a trap in the existing
# `warm.client.try_warm_dispatch` contract: that function's own docstring
# says ANY well-formed JSON-RPC response counts as a served warm hit,
# INCLUDING AN ERROR ENVELOPE (the anti-storm table's `well-formed JSON-RPC
# response, including an error envelope -- server up; USE the response`
# row). A guard-shaped caller (e.g. a Bash PreToolUse hook) that dispatches
# an op name the warm server has never registered gets back exactly that
# shape -- a real, well-formed `error` envelope with `code ==
# ipc.METHOD_NOT_FOUND` (-32601) -- and `try_warm_dispatch` alone cannot
# tell that apart from a genuine guard verdict the op computed on purpose.
# Mistaking the former for the latter corrupts the result of every Bash
# call routed through it.
#
# `try_warm_guard_dispatch` below is the seam that makes that distinction
# explicit: it treats a `METHOD_NOT_FOUND` error envelope as "the warm
# server does not know this op" -- a cold fall-through, not a hit -- and
# everything else `try_warm_dispatch` would return (a real result, or any
# OTHER error envelope, which is a legitimate answer the op computed) as a
# genuine warm hit.
#
# `METHOD_NOT_FOUND` is redefined locally rather than imported from
# `coordinator_core.ipc` (`ipc.METHOD_NOT_FOUND == -32601`): `ipc` is
# neither small nor free to import (`warm.client`'s own module docstring
# measures pulling the op-registry chain at ~330ms over a 40ms interpreter
# floor), and this module already sits on hot per-call paths. The value is
# a JSON-RPC 2.0 §5.1 reserved code, not project-specific, so duplicating
# the constant does not risk drifting out of sync with a project decision
# -- only with the JSON-RPC spec itself.
#
# Negative-spec (RAG-bait):
#     This module does NOT decide which op to dispatch, does NOT retry, and
#     does NOT add a live caller anywhere in the guard/hook path -- that is
#     C14b's job, gated on a warm-side op plus a change to the hook
#     invocation site in the DoE-claude repo (PM-gated, out of scope here).
#     It also makes NO measurement claim: AC13's <50ms number belongs to
#     C14b, not to this inert primitive.
# ---------------------------------------------------------------------------

#: JSON-RPC 2.0 §5.1 reserved code for "the method does not exist / is not
#: available" -- mirrors `coordinator_core.ipc.METHOD_NOT_FOUND` (-32601)
#: without importing `ipc`. See this section's module-docstring note above
#: for why the duplication is deliberate rather than an oversight.
METHOD_NOT_FOUND = -32601


@dataclass(frozen=True)
class WarmGuardOutcome:
    """The result of one `try_warm_guard_dispatch` attempt.

    `hit=True` means the warm server genuinely answered the dispatched op
    -- `response` is the full JSON-RPC envelope (a result OR an
    op-computed error, verbatim) and the caller should use it instead of
    falling through to a cold path. `hit=False` means every other outcome
    `warm.client.try_warm_dispatch` can produce (warmth disabled, no pipe,
    a busy/contended server, a malformed response, an unhandled exception,
    a `METHOD_NOT_FOUND` envelope) -- `response` is always `None` in that
    case, and the caller falls through to its existing cold path exactly
    as it would on any other warm miss.
    """

    hit: bool
    response: Optional[dict] = None


def _trigger_listener_boot() -> None:
    """Best-effort, cold-guard-path nudge toward a live http listener (C3 of
    docs/plans/2026-08-25-the-http-listener-gets-something-keeping-it-up.md).

    C2 gives the PIPE server's own boot a call to `supervisor.ensure_listener()`;
    this covers the box that boot does not -- neither process running, the
    ordinary state after any quiet period under idle demotion. Every call here
    costs one discovery-file read when a listener is already live, or a
    `should_spawn`-debounced spawn trigger when it is not; `ensure_listener`
    itself never waits and never raises by contract (its own docstring).

    GATED ON `is_engine_root`, DELIBERATELY -- `ensure_listener` does not gate
    on this itself (verified live: it happily `spawn_detached()`s an unstamped
    tree), but this seam does, for the same reason every other trust boundary
    in this package treats "stamped build" as the definition of an engine
    (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md). Production
    hook traffic runs through the klabauter publish clone, a stamped build
    (this repo's own CLAUDE.md); the bare dev tree this module also runs from
    (unstamped by design) is not that surface. Without this gate, EVERY
    existing caller of `try_warm_guard_dispatch` in an unstamped dev clone --
    including this suite's own tests, none of which mock `supervisor.
    ensure_listener` -- would trigger a real detached process spawn against
    the operator's real machine on each test run: exactly the litter class
    `test_warm_suite_does_not_litter_the_real_runtime_base.py` exists to
    catch, just reached through a path that guard does not cover.

    Never raises, never waits: every exception (an unresolvable engine root,
    an unimportable `supervisor` module, anything `ensure_listener` itself
    fails to absorb) is swallowed here, mirroring `try_warm_guard_dispatch`'s
    own fail-open contract -- this trigger must never become a new failure
    mode for the caller it decorates.
    """
    try:
        from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root

        root = current_engine_clone()
        if not is_engine_root(root):
            return

        from coordinator_core.warm import supervisor

        supervisor.ensure_listener(root)
    except Exception:  # noqa: BLE001 -- fail-open: this trigger must never fail the caller
        pass


def try_warm_guard_dispatch(
    op_name: str,
    params: Optional[dict] = None,
    *,
    request_id: Any = 1,
) -> WarmGuardOutcome:
    """Attempt one warm dispatch for a guard/hook-shaped caller, distinguishing
    a genuine warm hit from a `METHOD_NOT_FOUND` envelope the caller could
    otherwise mistake for one (see this module's section docstring above).

    FAILS OPEN on every failure mode: warmth disabled, no door (socket/pipe
    absent), the server refusing or timing out, a malformed or non-JSON-RPC
    response, an unregistered op, and any unanticipated exception -- all
    resolve to `WarmGuardOutcome(hit=False, response=None)`, never a raise.
    This function itself adds no new failure mode beyond what `warm.client.
    try_warm_dispatch` already fails open on; it only narrows what counts
    as a hit.

    `warm.client` is imported lazily, at call time, not at this module's
    top level: `client.py` performs real (if lightweight) work at import
    time in some environments and this primitive must stay inert -- an
    import failure here is itself just another fail-open case, not a
    reason to crash the caller.

    Also fires `_trigger_listener_boot()` -- the http listener's own
    autostart nudge (C3, see that function's docstring) -- best-effort and
    before the dispatch attempt itself, so a failure or slowness in THIS
    call's own warm dispatch can never suppress the listener nudge, and the
    nudge can never delay or fail this call's own result.
    """
    _trigger_listener_boot()

    try:
        from coordinator_core.warm.client import try_warm_dispatch
    except Exception:
        return WarmGuardOutcome(hit=False)

    msg = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": op_name,
        "params": params or {},
    }

    try:
        response = try_warm_dispatch(msg)
    except Exception:
        # Backstop, mirroring `try_warm_dispatch`'s own never-raise contract:
        # that function already catches everything internally, but this
        # primitive must never depend on that discipline holding forever.
        return WarmGuardOutcome(hit=False)

    if not isinstance(response, dict):
        return WarmGuardOutcome(hit=False)

    error = response.get("error")
    if isinstance(error, dict) and error.get("code") == METHOD_NOT_FOUND:
        # THE TRAP THAT BLOCKED C14: a well-formed error envelope about an
        # op the server has never heard of is not a guard verdict -- it is
        # the server telling us it cannot answer this question at all.
        # Treated as a cold fall-through, never as a hit.
        return WarmGuardOutcome(hit=False)

    return WarmGuardOutcome(hit=True, response=response)


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
