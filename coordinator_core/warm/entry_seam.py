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
    not resolve session identity, and does not record anything to disk. It
    delegates collection to `session.declared_writes.collecting()` (already
    `ContextVar` Token/reset-scoped, so nesting is safe) and recording
    remains each caller's own job via `ipc._record_self_reported_touches` —
    exactly the split `cli_entry.recording_declared_writes` already had.
    This module introduces no second declare/record dialect; it is a
    convergence point for the SCOPING half only.

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
from typing import Any, Iterator, List, Optional

from coordinator_core.session.declared_writes import collecting

__all__ = ["per_request_state", "reentrant_dispatch"]


@contextlib.contextmanager
def per_request_state(into: Optional[List[str]] = None) -> Iterator[List[str]]:
    """Open one request's worth of explicit, Token/reset-scoped state.

    Currently a thin pass-through to `session.declared_writes.collecting()`
    — the one per-request mechanism every entry path needs to bind
    explicitly under warmth rather than inherit ambiently. Named and kept
    as its own seam (rather than callers importing `collecting` directly)
    so a future per-request concern is a seam to grow HERE, once, instead
    of a parallel context manager invented at each entry path.

    `into`, when given, is the list `declare_write()` calls append to
    (matching `collecting()`'s own signature) — callers that already hold a
    list object to hand to a recorder (e.g. `cli_entry.recording_declared_
    writes`) pass it through unchanged rather than this seam allocating a
    second one.
    """
    with collecting(into) as declared:
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
