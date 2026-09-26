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

import contextlib
import contextvars
import os
from typing import Any, Iterator, List, Mapping, Optional

from coordinator_core.session.declared_writes import collecting


__all__ = [
    "per_request_state",
    "reentrant_dispatch",
    "emit_diagnostic",
    "collecting_diagnostics",
    "WarmGuardOutcome",
    "try_warm_guard_dispatch",
]


# Per-request DIAGNOSTIC axis (2026-08-19).
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
    sink: List[str] = [] if into is None else into
    token = _DIAGNOSTICS.set(sink)
    try:
        yield sink
    finally:
        _DIAGNOSTICS.reset(token)


# `os.environ` IDENTITY BORROW (C3, docs/plans/2026-08-30-every-op-runs-in-
# `CLAUDE_PID` is borrowed whenever `isolated`, on the SAME terms as the two
from coordinator_core.warm.env_forwarding import (
    BORROW,
    CALLER,
    FORWARDING_SET,
    OVERRIDE,
    REFUSE,
    is_caller_prefixed,
)

_ENV_LOWER_TIER_SESSION_NAMES = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
_ENV_TOP_TIER_SESSION_NAME = "COORDINATOR_SESSION_ID"
_ENV_ALL_SESSION_NAMES = (_ENV_TOP_TIER_SESSION_NAME,) + _ENV_LOWER_TIER_SESSION_NAMES
_ENV_CLAUDE_PID_NAME = "CLAUDE_PID"
_ENV_SETTINGS_HOME_NAME = "COORDINATOR_SETTINGS_HOME"

# THE THREE MODE GROUPS (C4, env_forwarding.FORWARDING_SET's own SSOT) --
# named tuples, not re-hand-written lists, so a name added to `FORWARDING_SET`
# edit here. Ordering within each group is `FORWARDING_SET`'s own declared
# existing precedence order, then borrow entries) -- `OVERRIDE_NAMES`
REFUSE_NAMES = tuple(e.name for e in FORWARDING_SET if e.mode == REFUSE)
OVERRIDE_NAMES = tuple(e.name for e in FORWARDING_SET if e.mode == OVERRIDE)
BORROW_NAMES = tuple(e.name for e in FORWARDING_SET if e.mode == BORROW)
CALLER_NAMES = tuple(e.name for e in FORWARDING_SET if e.mode == CALLER)

# `_ENV_BORROWED_NAMES` -- THE RESTORE SET `_environ_identity_borrow`'s
# exactly one hand-added name: `CLAUDE_PID` is RESTORE-scoped (this borrow
# WRITES `os.environ["CLAUDE_PID"]` from `caller_pid` below, so the
# FORWARD-scoped (it is not on the wire as a name in `FORWARDING_SET` --
# `env_forwarding.py`'s own negative spec: "`CLAUDE_PID` is deliberately NOT
_ENV_BORROWED_NAMES = tuple(e.name for e in FORWARDING_SET) + (_ENV_CLAUDE_PID_NAME,)


def _session_id_from_env(env: Optional[Mapping[str, str]]) -> Optional[str]:
    """The first non-empty value found walking `OVERRIDE_NAMES` in their
    declared precedence order, or `None`.

    Pure lookup, no UUID shape gate here -- callers that need the gate
    (`_environ_identity_borrow`'s OVERRIDE branch, and `per_request_state`'s
    own `session_identity_override` bind) apply `session.core._UUID_RE`
    themselves, since the ContextVar bind needs this same candidate even
    when `isolated=False` (unlike the `os.environ` mirror, which is
    isolated-gated).
    """
    if not env:
        return None
    for name in OVERRIDE_NAMES:
        value = env.get(name)
        if isinstance(value, str) and value:
            return value
    return None


@contextlib.contextmanager
def _environ_identity_borrow(
    env: Optional[Mapping[str, str]],
    isolated: bool,
    caller_pid: Optional[str],
) -> Iterator[None]:
    """Mirror the caller's carried identity into `os.environ` for the life
    of one ISOLATED dispatch, restored in a `finally` regardless of how the
    block exits.

    `isolated=False` is a complete no-op -- `os.environ` is never touched,
    read or written -- which is what keeps the `BrokenProcessPool` fallback
    (a real accept-thread dispatch, sharing this process with every other
    connection) from borrowing an identity into state every other in-flight
    request's ambient-env readers would also observe.

    `env` is the ONE declared-env-set axis (C4): a name->value mapping of
    whichever `env_forwarding.FORWARDING_SET` entries the caller's wire
    carried, resolved by `caller_context` from either wire shape
    (`warm.server`'s dual-read) before this function ever sees it. FOUR
    EXPLICIT BRANCHES, one per mode, mirroring `FORWARDING_SET`'s own four
    modes -- never collapsed into one handling path (DR-404's negative spec,
    satisfied here by branch shape, not by carrying `mode` as C-facing
    data -- see `env_forwarding.py`'s own module docstring):

      - `REFUSE_NAMES` (`COORDINATOR_SETTINGS_HOME` only): the PRE-DISPATCH
        refusal itself lives one layer up, in `warm.server._run_dispatch`'s
        own settings-home gate (`isolated=False` only -- an isolated caller
        never reaches that gate, per that function's own docstring). Here,
        under `isolated=True`, the claimed home is instead MIRRORED for the
        block's duration on the same shape-gate-or-pop terms as a borrow
        entry: non-empty and `os.path.isabs`, else popped -- never `stat`-ed
        (no existence check).
      - `OVERRIDE_NAMES` (the session-id precedence triple): re-validated
        against the same UUID shape `session_identity_override` gates on
        (`session.core._UUID_RE`) rather than trusted as already-valid -- a
        caller-supplied value that failed that gate must be treated as "no
        carried identity" here too. The first name in `OVERRIDE_NAMES`
        (top-tier) is bound when valid; every name in `OVERRIDE_NAMES` is
        popped otherwise, matching the pre-C4 behaviour byte-for-byte.
      - `BORROW_NAMES` (machine-constant entries, e.g.
        `MACHINE_LOCAL_REGISTRY_DIR`): shape-gate-or-pop, non-empty only (no
        per-entry gate is declared beyond presence -- `env_forwarding.py`'s
        own negative spec: "A per-entry shape-gate field is likewise not
        carried"). Absent from `env` is inherit-on-absent.
      - `CALLER_NAMES` (per-caller entries, e.g. `CLAUDE_PROJECT_DIR`): the
        same shape-gate-or-pop, but absent from `env` is POPPED -- the
        server's own value is its spawner's, never this caller's.

    `caller_pid` is the calling process's own id as carried on the wire
    (`caller_context.CallerContext.pid`). Bound to `CLAUDE_PID` when it is a
    decimal digit string; absent, `None`, or any other shape pops the name
    instead, which is the pre-existing behaviour and never a fabricated
    value. See `per_request_state`'s `caller_pid` parameter for why this is
    its own axis rather than a field of the declared env set: `CLAUDE_PID`
    is deliberately not a `FORWARDING_SET` entry (see `_ENV_BORROWED_NAMES`'s
    own comment above).
    """
    if not isolated:
        yield
        return

    from coordinator_core.session.core import _UUID_RE

    env = env or {}
    saved = {name: os.environ.get(name) for name in _ENV_BORROWED_NAMES}
    saved.update({name: value for name, value in os.environ.items() if is_caller_prefixed(name)})
    carried_prefixed = {
        name: value
        for name, value in env.items()
        if is_caller_prefixed(name) and isinstance(value, str) and value
    }
    try:

        # this is not the refusal itself). ABSENT FROM `env` ENTIRELY is
        for name in REFUSE_NAMES:
            if name not in env:
                continue
            value = env.get(name)
            if isinstance(value, str) and value and os.path.isabs(value):
                os.environ[name] = value
            else:
                emit_diagnostic(
                    f"{name} override rejected (not an absolute path): {value!r} -- "
                    "falling back to the server's own value"
                )
                os.environ.pop(name, None)

        # OVERRIDE branch (session-id precedence triple, unchanged UUID gate).
        valid_sid = _session_id_from_env(env)
        if valid_sid and not _UUID_RE.fullmatch(valid_sid):
            valid_sid = None
        if valid_sid:
            os.environ[OVERRIDE_NAMES[0]] = valid_sid
            for name in OVERRIDE_NAMES[1:]:
                os.environ.pop(name, None)
        else:
            for name in OVERRIDE_NAMES:
                os.environ.pop(name, None)

        # `MACHINE_LOCAL_REGISTRY_DIR`) -- same inherit-on-absent contract as
        for name in BORROW_NAMES:
            if name not in env:
                continue
            value = env.get(name)
            if isinstance(value, str) and value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

        for name in CALLER_NAMES:
            value = env.get(name)
            if isinstance(value, str) and value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

        # PREFIX branch (`env_forwarding.CALLER_PREFIXES`, the per-session
        for name in [n for n in os.environ if is_caller_prefixed(n)]:
            os.environ.pop(name, None)
        os.environ.update(carried_prefixed)

        if caller_pid is not None and caller_pid.isdigit():
            os.environ[_ENV_CLAUDE_PID_NAME] = caller_pid
        else:
            os.environ.pop(_ENV_CLAUDE_PID_NAME, None)
        yield
    finally:
        for name in [n for n in os.environ if is_caller_prefixed(n) and n not in saved]:
            os.environ.pop(name, None)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def per_request_state(
    into: Optional[List[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    diagnostics: Optional[List[str]] = None,
    warm_served: Optional[bool] = None,
    caller_pid: Optional[str] = None,
    session_id: Optional[str] = None,
    settings_home: Optional[str] = None,
    isolated: bool,
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

    `env` (C4) is the ONE axis every `FORWARDING_SET` entry now rides,
    superseding the separate `session_id` and `settings_home` parameters
    this signature carried before C4: a name->value mapping of whichever
    `env_forwarding.FORWARDING_SET` entries the caller's wire carried,
    already resolved by `warm.caller_context`'s dual-read (the new
    envelope-level `_env` object, or a legacy-shape caller's `_caller`/
    `_settings_home` fields folded onto the same shape) before this function
    ever sees it. `session_id` and `settings_home` are KEPT, not removed --
    the ~20 non-`warm.server` call sites across `coordinator_core/session/`
    and `coordinator_core/ops/` predate C4 and are out of this chunk's
    `writes:` scope, so breaking their signature is not this row's call to
    make. Given ALONGSIDE `env`, each is folded onto it as one more
    `FORWARDING_SET`-shaped entry (top-tier session name, `COORDINATOR_
    SETTINGS_HOME`) before the mode dispatch below ever runs -- `env` wins
    on a key both supply, so a caller migrated onto the new axis is never
    silently overridden by a stale positional habit. `warm.server`'s own two
    production call sites pass `env=` alone, matching the plan's design;
    every other production and test call site is unaffected. Two independent
    uses of the merged mapping, both inherit-on-absent (an empty/`None` map
    is a no-op on both):

      - The session-id candidate (`_session_id_from_env`, walking
        `env_forwarding.OVERRIDE_NAMES` precedence) is bound via
        `session_identity_override` UNCONDITIONALLY -- on every `isolated`
        leg -- exactly as the former `session_id` parameter was: this is a
        thread-safe ContextVar bind, not an `os.environ` mutation, so it
        needs no process isolation.
      - The full `env` mapping is threaded to `_environ_identity_borrow`,
        which performs the four-branch REFUSE/OVERRIDE/BORROW/CALLER mode dispatch
        against it, `isolated=True` only. See that function's own docstring
        for the per-mode handling this seam does not re-derive here.

    `diagnostics`, when given, is the list `emit_diagnostic()` calls append to
    for the duration of the block — the third axis (see the DIAGNOSTIC-axis
    note above). Omitted/`None` binds no sink, so `emit_diagnostic` stays a
    no-op and a caller that does not ask for diagnostics is unaffected.

    `warm_served` is the fourth axis: True marks the block as running inside
    a warm-server dispatch (`session.core.in_warm_served_request()`). It is a
    SEPARATE axis from `env` on purpose, because the case that needs
    it is precisely the one where no session id is carried — a request served
    warm through a door that sent no session name leaves the identity
    ContextVar unset, which is indistinguishable from a cold invocation, and
    the two want opposite fallbacks. Inherit-on-absent, matching this seam's
    other axes: `None` (the default) binds nothing and leaves the
    outer scope's flag untouched, so `cli_entry`'s cold call site (the only
    other production caller) and any nested re-entrant open both inherit the
    enclosing scope's value with no action required; only an explicit
    True/False forces the flag. The two warm dispatch sites in `warm.server`
    pass `warm_served=True`.
    <!-- Review: overengineering-reviewer (finding 1) — the prior
    force-bind default (`False`) was the one axis on this seam that did not
    inherit-on-absent, and that divergence is what forced
    `reentrant_dispatch` to re-thread the outer flag by hand. -->

    `caller_pid` is the fifth axis: the CALLING process's own id, carried on
    the wire since C1b and mirrored into `CLAUDE_PID` for the block's
    duration under `isolated=True` only (`_environ_identity_borrow`, same
    `finally` restore as the declared-set names). It is what `harness_registry.
    self_record()` -- which keys off the pid and not off the session id --
    needs in order to classify an isolated dispatch as the CALLER rather than
    as the engine owner. Omitted/`None` pops the name, the behaviour every
    caller had before this axis existed.

    ITS OWN AXIS RATHER THAN A KEY IN `env`, which is the question
    the module comment above defers here: `self_record()` keys off the pid
    ALONE, so a request carrying one and not a session id must still close
    the defect it can, and `CLAUDE_PID` is deliberately not a
    `FORWARDING_SET` entry at all (`env_forwarding.py`'s own negative spec --
    it is derived from `GetCurrentProcessId()`/`getpid()`, never read from
    the environment). Passing the whole `CallerContext` was the other
    candidate and is worse here: several of its fields have no consumer in
    this seam, and it would import `warm.caller_context` into a surface ~47
    call sites reach.

    `isolated` is a REQUIRED keyword-only argument, with no default: every
    call site must declare its own execution shape rather than inheriting a
    silently-safe one. `True` means this dispatch owns a process no
    concurrent request shares (the `DISPATCH_PROCESS_POOL_SIZE` pool worker
    target, `warm.server._pool_dispatch_worker`, on both transport legs) and
    additionally mirrors the caller's carried identity into `os.environ` for
    the block's duration (`_environ_identity_borrow`, restored in a
    `finally`) — the compatibility mirror ambient-env readers (e.g.
    `harness_registry.self_record()`, `subprocess_identity_env()`'s callers)
    need, safe here only because process isolation means no OTHER concurrent
    request's ambient-env read can observe this process's mutated
    environment. `False` — every other caller, including the cold path
    (`os.environ` there already IS the caller's own, so the borrow would be a
    no-op that costs a dict copy) and the `BrokenProcessPool` degrade path
    (a real, unisolated accept-thread dispatch sharing this process with
    every other in-flight connection) — takes no env borrow at all; the
    ContextVar bind above (`session_identity_override`) still happens
    unconditionally on every leg, since that bind is thread-safe regardless
    of process isolation.
    """
    from coordinator_core.session.core import (
        session_identity_override,
        warm_served_request,
    )

    warm_scope: "contextlib.AbstractContextManager[object]"
    if warm_served is None:
        warm_scope = contextlib.nullcontext()
    else:
        warm_scope = warm_served_request(bool(warm_served))

    # onto `env` as one more `FORWARDING_SET`-shaped mapping -- see this
    merged_env: Optional[Mapping[str, str]] = env
    if session_id is not None or settings_home is not None:
        merged_env = dict(env) if env else {}
        if session_id is not None and OVERRIDE_NAMES and OVERRIDE_NAMES[0] not in merged_env:
            merged_env[OVERRIDE_NAMES[0]] = session_id
        if settings_home is not None and REFUSE_NAMES and REFUSE_NAMES[0] not in merged_env:
            merged_env[REFUSE_NAMES[0]] = settings_home

    diagnostics_scope: "contextlib.AbstractContextManager[object]"
    if diagnostics is None:
        diagnostics_scope = contextlib.nullcontext()
    else:
        diagnostics_scope = collecting_diagnostics(diagnostics)

    with warm_scope, session_identity_override(_session_id_from_env(merged_env)):
        with diagnostics_scope:
            with _environ_identity_borrow(merged_env, isolated, caller_pid):
                with collecting(into) as declared:
                    yield declared


# Warm-first CLIENT PRIMITIVE (C14a, no live caller yet).
# says ANY well-formed JSON-RPC response counts as a served warm hit,
# INCLUDING AN ERROR ENVELOPE (the anti-storm table's `well-formed JSON-RPC
# ipc.METHOD_NOT_FOUND` (-32601) -- and `try_warm_dispatch` alone cannot
# explicit: it treats a `METHOD_NOT_FOUND` error envelope as "the warm
# `METHOD_NOT_FOUND` is redefined locally rather than imported from
# `coordinator_core.ipc` (`ipc.METHOD_NOT_FOUND == -32601`): `ipc` is
# a JSON-RPC 2.0 §5.1 reserved code, not project-specific, so duplicating
# -- only with the JSON-RPC spec itself.

#: JSON-RPC 2.0 §5.1 reserved code for "the method does not exist / is not
#: available" -- mirrors `coordinator_core.ipc.METHOD_NOT_FOUND` (-32601)
METHOD_NOT_FOUND = -32601


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

    WRITTEN OUT BY HAND RATHER THAN AS A `@dataclass(frozen=True)`, which
    is what it was until 2026-08-31. `dataclasses` pulls `copy` and
    `inspect` (and so `ast`, `dis`, `tokenize`) in behind it -- ~6.5ms
    against the 20ms import-CPU ceiling
    `tests/test_warm_reach_import_ceiling.py` pins on this entry point,
    spent on one immutable two-field record. The generated `__init__`,
    `__eq__`, `__hash__` and `__repr__` are reproduced below with the same
    semantics; whole-outcome `==` comparison is contract, not convenience
    (the entry-seam suites assert on it).
    """

    __slots__ = ("hit", "response")

    def __init__(self, hit: bool, response: Optional[dict] = None) -> None:
        object.__setattr__(self, "hit", hit)
        object.__setattr__(self, "response", response)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is frozen: cannot set {name!r}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is frozen: cannot delete {name!r}")

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.hit, self.response) == (other.hit, other.response)

    def __hash__(self) -> int:
        return hash((self.hit, self.response))

    def __repr__(self) -> str:
        return f"WarmGuardOutcome(hit={self.hit!r}, response={self.response!r})"


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
        return WarmGuardOutcome(hit=False)

    if not isinstance(response, dict):
        return WarmGuardOutcome(hit=False)

    error = response.get("error")
    if isinstance(error, dict) and error.get("code") == METHOD_NOT_FOUND:
        return WarmGuardOutcome(hit=False)

    return WarmGuardOutcome(hit=True, response=response)


def reentrant_dispatch(
    op_name: str,
    params: dict,
    *,
    repo_root: Optional[Any] = None,
) -> Any:
    import asyncio

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

    with per_request_state(isolated=False):
        return handler(params, repo_root=repo_root)
