"""coordinator_core.warm.client — client-side warm-pipe preamble.

Purpose: C15 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md. The
warm preamble `coordinator_core.invoke.__main__.main()` runs BEFORE its
existing (verbatim, unmodified) cold dispatch path: resolve this engine
clone's server pipe name, skip straight to cold when warmth is disabled,
attempt one framed-NDJSON request/response over the pipe, and fall back to
cold on any outcome other than a well-formed, non-skewed JSON-RPC response.

NO CLIENT EVER WAITS FOR A SERVER TO BOOT -- design change 2026-08-15
(this chunk's own plan body), superseding an earlier "spawn then poll to
START_DEADLINE 2.0s at 25ms". The only trigger this module ever spawns on
is `FileNotFoundError` (no pipe -- nobody home), and a spawn is followed
IMMEDIATELY by going cold this call, never by waiting or retrying the
open. Every other outcome in the table below goes cold with no spawn.
Rationale (not re-litigated here): with C24's idle demotion, the no-pipe
case stops being a rare cold start and becomes the ordinary first call
after every quiet period -- waiting would turn the cool-down into a
recurring latency penalty. Spawn-and-go-cold costs exactly ONE cold
invocation (the same ~273ms p50 a client would pay with no warm engine at
all); every call after it is warm.

THE ANTI-STORM TABLE (verbatim intent from the plan body -- this table IS
the chunk, not a detail):
    FileNotFoundError (errno 2)      -- THE ONLY SPAWN TRIGGER, then cold.
                                         Also what a genuinely-absent pipe
                                         AND a momentarily pool-starved one
                                         (fewer listening instances than
                                         simultaneous openers) both surface
                                         as -- see `warm.server`'s
                                         `PENDING_LISTENER_POOL_SIZE`
                                         (problem 3 of the 2026-08-19
                                         concurrency problem set), which
                                         closes the pool-starved case at
                                         its source rather than asking this
                                         table to tell the two apart after
                                         the fact.
    OSError winerror == 231          -- ERROR_PIPE_BUSY: server up, go cold
                                         this call, never spawn. Verified
                                         live on this box 2026-08-15: does
                                         NOT surface as a named exception
                                         subclass -- plain OSError,
                                         winerror=231, errno=22, because
                                         CPython's winerror-to-errno table
                                         has no entry for 231. MUST branch
                                         on `e.winerror`, never on
                                         exception class, or a busy-server
                                         OSError misclassifies as "no
                                         pipe" and re-triggers the ~49-
                                         process spawn storm this table
                                         exists to prevent.
    OSError errno == EINVAL (22),
    winerror != 231                  -- a second, distinct contended-pipe
                                         outcome measured under 16-way
                                         concurrency (~8% of failed opens,
                                         docs/research/warm-engine-premise/
                                         warm-under-concurrency.md), never
                                         reproduced with a winerror of 231.
                                         Treated the same as ERROR_PIPE_BUSY
                                         -- server up, contended, go cold
                                         this call, never spawn -- rather
                                         than falling through to Backstop 2,
                                         which used to print a diagnostic to
                                         stderr on this exact hot path for
                                         every occurrence (problem 4).
    PermissionError (winerror 5)     -- someone else's pipe. Never spawn.
    BrokenPipeError mid-request      -- one re-open, then cold.
    well-formed JSON-RPC response,
    including an error envelope      -- server up; USE the response.
    ENGINE_SKEW (-32002)             -- server up and stale: go cold.
    read-deadline expiry             -- go cold.
    Backstop 1: ONE spawn attempt per client process, ever.
    Backstop 2: THIS PREAMBLE never fails the op -- every row in this table
        resolves to a served response or a clean `None`, never a raised
        exception. That mechanical guarantee stands, unchanged.

        RETIRED 2026-08-21 (PM ruling, state/handoffs/2026-08-21_103635_
        reaching-the-warm-engine.md, verbatim: "I'd rather have a fail than
        a silent slow. Much rather."): what is retired is NOT this table's
        own never-raise contract -- it is the CALLER'S assumption that a
        `None` here is always safe to fall through to a cold spawn.
        "The cold path is a SUCCESS path" described `coordinator_core.
        invoke.__main__._dispatch_argv_body`'s OWN behaviour, documented
        here because this preamble's whole design leaned on that caller
        always having a safe landing. It no longer does: that function now
        fails hard on a `None` here (when warm is enabled and the caller
        did not opt into manual-testing mode via
        `ipc.is_unstamped_dispatch_allowed()`) instead of degrading to a
        slow cold spawn. See `invoke.__main__`'s own "6a. Warm preamble"
        comment for the enforcement half of this retirement -- this module
        itself needed no code change, only this notice: `try_warm_dispatch`
        was already returning the same honest `None` this policy now acts
        on differently.

Caller-identity seam: `_caller_session_id()` resolves THIS client process's
own session id (`coordinator_core.session.core.resolve_session_id()`) --
cold, in the true caller's own environment, before the request ever
crosses the pipe -- and the request carries it beside `_engine_token` under
`_session_id`, mirroring that field's own naming and "absent means
degrade, never fabricate" handling. Exists to close the warm-engine
identity-attribution defect (state/bug-backlog/2026-08-18-a-warm-server-
stamps-every-op-it-serves-eeb801fc6bee.yaml): a long-lived warm server's
own `os.environ` reflects whoever SPAWNED it, not the caller of any given
request, so identity must cross the wire explicitly rather than being
re-resolved server-side. Unresolvable (empty string) is omitted from the
request entirely, never sent as `""` or fabricated -- the server-side
`session.core.session_identity_override` no-ops on absence exactly like
`resolve_session_id()`'s own env chain does today, so an older client (or
a caller this process cannot identify) degrades to the server's
pre-existing behaviour, not a broken one.

Engine-token seam: `engine_token()` is a placeholder. C16 ("Version tokens
and skew eviction", dispatched after this chunk) owns computing the real
skew-signal value and is free to replace this function's body outright.
Its name and zero-argument signature are the pinned seam the rest of this
module (pipe-name resolution, the request envelope) binds to; nothing else
here assumes anything about how the token is derived.

Server-spawn seam: `SERVER_ENTRY_SCRIPT` names the script `spawn_detached`
execs on the FileNotFoundError trigger. C18 ("Spawn, breadcrumb, and the
runnable stop script") owns building that script and layering the
breadcrumb spawn-debounce in front of this call. Until C18 lands, spawning
a not-yet-existing script is harmless by construction:
`spawn_detached`'s own contract is "never raise", asserts only that
`Popen` itself did not fail, and never observes or waits on the spawned
child (`coordinator_core.ops.ceremony.detached_spawn` module docstring).
That is exactly this chunk's own contract too -- attempt a spawn, then go
cold immediately, without ever depending on whether the spawn's target
exists yet.

Wire format: NDJSON + JSON-RPC 2.0 (`coordinator_core.ipc` Decision D1) --
one compact JSON object, UTF-8, newline-terminated, per direction. The
client-side transport API is the spike's measured shape
(docs/research/spike-verdicts/2026-08-14-stdlib-named-pipe-server-on-windows.md):
plain `open(pipe, "r+b")`, no ctypes client-side.

Negative-spec:
    - Does NOT wait for a server to boot, in any form -- no poll loop, no
      retry-until-ready, no boot deadline. See module docstring above.
    - Does NOT implement the breadcrumb spawn-debounce -- C18's job.
    - Does NOT compute the real engine-generation token -- C16's job.
    - Does NOT touch anything after the preamble in
      `coordinator_core.invoke.__main__.main()` -- the existing cold
      dispatch path there is unchanged; this module only supplies an
      optional, earlier-available `response`.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C15

AC3/AC4 (docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md § C3):
    - The in-process path above (`try_warm_dispatch`) keeps its existing
      module-level `election`/`is_warm_enabled` names UNCHANGED: both are
      the pinned monkeypatch surface `warm/tests/test_client_fallback.py`
      patches by attribute (`client.is_warm_enabled`, `client.election.
      pipe_name`) on every test in that module, and caching or deferring
      either import here would either silently defeat that per-test
      monkeypatch (a cached result surviving across tests) or require
      editing that test file, which sits outside this chunk's `writes:`
      scope. The P2 cost these two calls carry on the in-process path is
      therefore NOT eliminated here -- it is a follow-up that needs the
      test file in scope alongside `client.py`.
    - The fourth P2 cost -- `op_latency`'s two NDJSON disk appends inside
      `ipc.dispatch_message` -- is likewise not edited here: that file is
      outside this chunk's `writes:` scope (C4 owns it). Per staff-data-sci
      review M6, `COORDINATOR_OP_LATENCY_DISABLE=1` is a ready-made CONTROL
      for that cost's own measurement (`op_latency._write_entry` checks it
      first and cheaply, hard-disabling both row kinds) -- named here as
      the control an AC4 measurement should use, not a fix landed in this
      file, and not itself usable to measure the instrument it disables.
    - The `_cli_*`/`_cli_main` section at the bottom of this file is the
      separate, stdlib-only, `-S -E`-runnable CLI artifact AC3 asks for.
      It is a pure ADDITION -- nothing above it calls into it and it calls
      nothing above it -- so it carries none of the in-process path's P2
      costs (no `election`/`settings` import at all, cached or not) without
      touching that path's pinned shape. See that section's own docstring
      for why it duplicates rather than imports `election`/`settings`/
      `skew`.
    - The `election`/`is_warm_enabled` import below is guarded on
      `__name__ != "__main__"` rather than moved inside a function: run as
      `import coordinator_core.warm.client` (every existing caller and
      test), `__name__` is the dotted module path, so this executes exactly
      as before -- unconditionally, at import time -- and every attribute
      `warm/tests/test_client_fallback.py` monkeypatches
      (`client.is_warm_enabled`, `client.election.pipe_name`) still exists
      to patch. Run DIRECTLY as a script (`python -S -E client.py ...`),
      `__name__` is `"__main__"`, so this import is SKIPPED -- which is the
      only way `-S -E` (no site, no `coordinator_core` on `sys.path`) can
      reach the `if __name__ == "__main__":` block at the bottom of this
      file at all: an unconditional `import coordinator_core....` here
      would raise `ModuleNotFoundError` under `-S -E` before that block
      ever ran.
"""

from __future__ import annotations

import errno
import json
import sys
import threading
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "ERROR_PIPE_BUSY",
    "ENGINE_SKEW",
    "READ_DEADLINE_SECS",
    "SERVER_ENTRY_SCRIPT",
    "engine_token",
    "try_warm_dispatch",
]

ERROR_PIPE_BUSY = 231

#: POSIX errnos that mean "the server is up and cannot take this connection
#: right now" -- never "the server is absent". They share ERROR_PIPE_BUSY's
#: disposition exactly (go cold, never spawn); the split exists only because
#: the two platforms report one condition through different fields.
_CONTENDED_ERRNOS = frozenset({errno.EAGAIN, errno.ECONNABORTED, errno.ECONNRESET})

#: Distinct from `coordinator_core.ipc.STRUCTURAL_PIN_ERROR` (-32001) on
#: purpose -- that code is a cross-repo surface (cc_invoke pins it, DoE reads
#: rc=2 off it) and MUST NOT move. `ENGINE_SKEW` crosses only our own pipe, so
#: it took the collision instead. Pinned distinct by
#: `coordinator_core/warm/tests/test_client_fallback.py`.
ENGINE_SKEW = -32002

#: `engine_root.current_engine_clone` is imported lazily, at call time,
#: inside `_engine_clone_root()` and `_cli_main` -- never at this module's
#: top level. `engine_root` imports `skew`, and `skew` re-exports
#: `ENGINE_SKEW` from THIS module via a lazy `__getattr__` (never a
#: module-scope import of `client`), because `client` itself imports
#: `election`, which imports `engine_root` -- a module-scope import in
#: either direction here closes `engine_root -> skew -> client -> election
#: -> engine_root` into a real cycle.
if __name__ != "__main__":
    from coordinator_core.warm import election
    from coordinator_core.warm.settings import is_warm_enabled

READ_DEADLINE_SECS = 2.0

#: Read deadline for a MUTATING op whose request has already been DELIVERED to
#: the server. `READ_DEADLINE_SECS` above is a liveness probe sized for "is the
#: server wedged" -- 2s is far below how long a real mutation takes on this box
#: (a `git commit` with the Python pre-commit hook in the path, at the stated
#: load norm of 50-70 concurrent sessions, routinely exceeds it). Applying the
#: liveness probe's deadline to a delivered mutation is what made the client
#: abandon a request the server was still EXECUTING and re-run it cold --
#: see `_MUTATION_INDETERMINATE_MESSAGE` for the incident.
#:
#: A slow op is not a wedged server -- BUT this wait also runs inside the
#: CALLER's own subprocess kill ceiling (`cc_invoke.py::_op_timeout_ceiling`,
#: `max(FLOOR=10, engine_budget(op) + MARGIN=10)`), and a flat 120s here blew
#: past that ceiling for every mutating op: `ipc.py::_OP_TIMEOUT_OVERRIDES` is
#: now empty (`ceremony.scoped_git_commit`'s 150s row was revoked 2026-08-21 by
#: the ceremony budget, `ipc.CEREMONY_BUDGET_SECS`), so every `ceremony.*` op
#: resolves to that 2s ceiling and every other op to `ipc.DISPATCH_TIMEOUT_SECS`
#: = 30s, giving the caller a ceiling well under 120s in either case, so this
#: client was killed by its own parent long before a flat 120s wait could ever
#: return the `WARM_DISPATCH_INDETERMINATE` envelope -- the exact protection
#: this deadline exists to guarantee, silently absent everywhere it was not the
#: incident's own op (WARN, coordinator-code-reviewer sidecar dcf219af, SLICE
#: 1). `_mutation_deadline_for` below derives the real wait per-op from
#: `ipc._timeout_for` -- the same source of truth the caller's own ceiling is
#: derived from -- so the wait stays inside that ceiling for every op, not just
#: the one this incident concerned. Deriving from that shared source of truth is
#: also why this client needed no change of its own when the ceremony budget
#: landed: `_mutation_deadline_for` inherits the 2s clamp automatically. This
#: constant is now only the fail-safe floor: 30.0 mirrors `ipc`'s own
#: `DISPATCH_TIMEOUT_SECS` default, so it can never itself outlive an
#: un-derivable op's caller ceiling.
MUTATION_READ_DEADLINE_SECS = 30.0

#: Frozen at import time, never reassigned -- the yardstick
#: `_mutation_deadline_for` compares `MUTATION_READ_DEADLINE_SECS` against to
#: tell "nobody touched the constant" from "something (a test, an operator)
#: reassigned it". Without this, a monkeypatched `MUTATION_READ_DEADLINE_SECS`
#: would be invisible: it is not consulted at all when the per-op derivation
#: succeeds.
#:
#: An override so detected may only NARROW (PM ruling 2026-08-21, no dial
#: raises). Honoring it verbatim -- what this seam did until that ruling --
#: skipped `ipc._timeout_for` entirely, and with it the `ceremony.*`
#: `CEREMONY_BUDGET_SECS` clamp every ceremony op is held to, so any
#: import-time reassignment of this module constant was a clamp bypass
#: reachable without touching `ipc` at all. `min(override, derived)` keeps
#: the seam that makes a test's lowered deadline real while removing the
#: direction that widens one.
_MUTATION_READ_DEADLINE_DEFAULT = MUTATION_READ_DEADLINE_SECS

#: JSON-RPC error code for "your mutation was delivered and its outcome is
#: unknown". Distinct from INTERNAL_ERROR so a caller can tell "the op failed"
#: from "the op may well have SUCCEEDED and we cannot see the answer".
WARM_DISPATCH_INDETERMINATE = -32004
SERVER_ENTRY_SCRIPT = "coordinator_core/warm/server.py"

_spawned_this_process = False

#: Set True the first time `engine_token()` observes `UnstampedEngineRootError`
#: in THIS process -- i.e. this clone is a live working tree, not a published
#: engine root. Gates `_spawn_once`: see `_log_live_tree_cold_once`'s
#: docstring for why a spawn from here is doomed, never merely undesirable.
_live_tree_cold = False

#: DR-315 s2 (PM ruling 2026-08-16, restated in CLAUDE.md) settles that the
#: warm server runs the published build, never the live working tree,
#: box-wide -- corroborated by DR-326 ("the engine does not resolve via
#: claude-klabauter") and DR-331 ("an engine root is a stamped build"), both accepted.
#: LIVE-TREE-COLD IS CORRECT BEHAVIOUR: this message exists so the code that
#: implements the ruling says so by name, instead of leaving a reader to
#: independently conclude, as a staff reviewer once did, that the question
#: is still open.
_LIVE_TREE_COLD_MESSAGE = (
    "warm engine: this clone carries no engine build stamp, so it is not a "
    "warm-server host -- by ruling, not by defect (DR-315 s2; corroborated "
    "by DR-326 and DR-331). Every call from this tree goes cold; no server "
    "will be spawned for it."
)

#: The last reason THIS process went permanently cold, as
#: `_log_live_tree_cold_once` phrased it -- read back by
#: `invoke.__main__`'s fail-hard block via `last_cold_reason()`.
#:
#: Without it the operator got both halves of a contradiction and neither
#: half named a path: this module's "every call from this tree goes cold",
#: immediately followed by `__main__`'s "cold fallback is disabled ... retry
#: in a moment" (observed 2026-08-22, every settings-home `bin/` live op on
#: `machine-b`). The retry advice is correct for a transient warm miss and
#: wrong for this one -- the condition is permanent for the process
#: population, and retrying it forever is what the operator actually did.
_cold_reason: "str | None" = None


def last_cold_reason() -> "str | None":
    """Why this process can never reach a warm server, or None if the last
    warm miss was an ordinary transient one (server booting, busy, evicted).

    A caller turning a warm miss into a fatal error uses this to choose
    between "retry in a moment" and a remediation that names the actual
    broken thing. Never raises and never computes: it reports what
    `_log_live_tree_cold_once` already observed."""
    return _cold_reason


def _live_tree_cold_message(exc: Exception) -> str:
    """The stderr line for a clone that can never host a warm server.

    TWO CONDITIONS, and conflating them is the defect being fixed. An
    unstamped tree that IS on disk is the ruling, and naming DR-315 there is
    the point (see `_LIVE_TREE_COLD_MESSAGE`). A resolved engine root that is
    NOT on disk is a broken root-resolution channel; DR-315 does not cover
    it, and citing the ruling for it sent an operator to a decision record
    instead of to the pointer file holding a path from another machine. That
    arm therefore names the path that failed to resolve and routes to the
    reader that shows which channel produced it.

    Only an explicit `root_exists=False` takes that arm. `root_exists` is
    `None` (unknown) on a bare-message construction and on any future call
    site that passes `root=` without `root_exists=`; unknown is not
    evidence of absence, so it falls through to the ruling-shaped message
    below, same as an explicit `True`.
    """
    root = getattr(exc, "root", None)
    if root is not None and getattr(exc, "root_exists", None) is False:
        return (
            f"warm engine: resolved engine root does not exist: {root}. No warm "
            "server can be hosted from it and none will be spawned. "
            "Remediation: python3 -m coordinator_core.root_channel_reconcile"
        )
    if root is not None:
        return f"{_LIVE_TREE_COLD_MESSAGE} Resolved engine root: {root}."
    return _LIVE_TREE_COLD_MESSAGE


def _engine_clone_root() -> Path:
    """This coordinator_core clone's own resolved root -- collapsed onto
    the single shared definition, `engine_root.current_engine_clone()`
    (plan 2026-08-19-an-engine-root-is-a-stamped-build § C3). Used as the
    `repo_root` `spawn_detached` logs housekeeping failures under.

    Import is deferred to call time, not module scope: `engine_root`
    imports `skew`, and `skew` imports `ENGINE_SKEW` back from this module
    at ITS module scope, so a module-level import here would close
    `client -> engine_root -> skew -> client` into a real cycle."""
    from coordinator_core.warm.engine_root import current_engine_clone

    return current_engine_clone()


def engine_token() -> str:
    """The engine-generation token `election.pipe_name` stamps into the pipe
    name, so a successor bound to a new generation binds immediately while
    the predecessor is still draining.

    Delegates to `skew.compute_client_token()`: sha1 over `(mtime_ns, size)`
    of `.git/HEAD` and of the ref it names. Two stats, no subprocess -- the
    client never pays `resolve_engine_sha()` (a `git rev-parse` at ~5-15ms),
    because paying a subprocess per call to avoid a subprocess per call is
    self-defeating. Server-side dirty detection is a separate axis and is
    NOT computed here.

    Import direction is client -> skew inside this function body only
    (deferred past module-load time); `skew` imports `ENGINE_SKEW` back from
    this module at its own module scope, which stays acyclic precisely
    because this edge is deferred, not because the two modules avoid
    importing each other.

    Falls back to the previous stable placeholder if the token cannot be
    computed. A token that raises would fail the preamble, and nothing in
    the preamble may fail in a way that fails the op (C15's contract); a
    constant token merely means skew eviction cannot fire, which degrades
    to today's behaviour rather than breaking the call.

    `UnstampedEngineRootError` is handled separately from every other
    failure here: it does not mean "token not yet known", it means this
    clone is a live working tree, which cannot host a warm server by
    ruling (see `_LIVE_TREE_COLD_MESSAGE`). Logged once per process and
    remembered on `_live_tree_cold`, which `_spawn_once` consults so this
    tree never wastes a spawn attempt on a server it can never boot.

    Spec backlink: plan 2026-08-16-one-engine-for-the-whole-box, C15 seam
    filled by C16. Live-tree-cold naming: plan
    2026-08-20-a-refusal-cannot-exit-zero.md § C3.
    """
    try:
        from coordinator_core.warm.skew import UnstampedEngineRootError, compute_client_token
    except Exception:
        return "unversioned"
    try:
        return compute_client_token()
    except UnstampedEngineRootError as exc:
        _log_live_tree_cold_once(exc)
        return "unversioned"
    except Exception:
        return "unversioned"


def _log_live_tree_cold_once(exc: "Exception | None" = None) -> None:
    """Emit the cold-forever reason on this process's stderr exactly once,
    and remember it on `_live_tree_cold` (for `_spawn_once`) and on
    `_cold_reason` (for whoever turns the miss into a fatal error).

    ONCE, deliberately: this is the permanent condition of a whole process
    population (every dispatch from an unstamped clone), not a per-call
    fact -- a per-call diagnostic here would be pure noise on the hot path
    this module sits on.

    `_cold_reason` is set on EVERY call, not only the first: it is read by a
    later caller that may run after the one-shot print already happened, and
    a reason that existed only for the first dispatch in a process would be
    missing from exactly the message that needs it."""
    global _live_tree_cold, _cold_reason
    message = _live_tree_cold_message(exc) if exc is not None else _LIVE_TREE_COLD_MESSAGE
    _cold_reason = message
    if not _live_tree_cold:
        print(f"[warm-client] {message}", file=sys.stderr)
    _live_tree_cold = True


def _caller_session_id() -> str:
    """This client process's own resolved session id, cold, in its own
    (true-caller) environment -- see module docstring's "Caller-identity
    seam". Never raises: `resolve_session_id()` itself never raises (its
    own docstring), and an import failure here is treated identically to
    "unresolvable" so this preamble never fails the op over an identity
    lookup (Backstop 2)."""
    try:
        from coordinator_core.session.core import resolve_session_id

        return resolve_session_id()
    except Exception:
        return ""


def spawn_detached(repo_root: str, script_path: str, args: Optional[Any] = None) -> bool:
    """Lazy delegate to `ops.ceremony.detached_spawn.spawn_detached`.

    THE IMPORT IS INSIDE THE FUNCTION ON PURPOSE — it is the single most
    expensive thing this module could do, and it was being done on every
    invocation of the engine. `detached_spawn` lives under
    `coordinator_core.ops`, so importing it executes `ops/__init__.py` and
    registers the ENTIRE op surface: measured on this box, importing this
    module pulled 527 `coordinator_core` modules (316 of them
    `coordinator_core.ops.*`) and cost ~330 ms over a 40 ms interpreter
    floor, against 4 modules and ~53 ms for `invoke.__main__` by itself.
    `detached_spawn`'s own imports are all stdlib; the whole cost is the
    registration chain reached through the package `__init__`.

    `invoke/__main__.py` imports this module unconditionally, ahead of its
    warm/cold branch, so that cost was paid by every call — warm hit and
    cold fallback alike, and even with `COORDINATOR_WARM=0`. The warm
    preamble was therefore paying, up front, the exact engine-import cost
    warmth exists to avoid, which is why a confirmed warm hit measured no
    faster than cold (221 ms vs 224 ms for CLI `ping`, 2026-08-19). It also
    silently undid `__main__`'s own "No eager `import coordinator_core.ops`"
    property, through a package-`__init__` side door rather than a direct
    import. Found by claude-klabauter-44.

    The only caller is `_spawn_once`, on the `FileNotFoundError` "no pipe,
    nobody home" branch — never on a warm hit, a disabled read, or any
    other cold-fallback outcome. So the registry now loads only when this
    process is actually about to start a server.

    NEGATIVE-SPEC: this wrapper is a MODULE-LEVEL NAME by design, not an
    inlined function-local import at the call site. `warm/tests/
    test_client_fallback.py` substitutes the spawn seam with
    `monkeypatch.setattr(client, "spawn_detached", ...)` at five sites; a
    function-local import at `_spawn_once` would bypass every one of them
    and quietly spawn real detached processes during the suite.
    """
    from coordinator_core.ops.ceremony.detached_spawn import (
        spawn_detached as _spawn_detached_impl,
    )

    return _spawn_detached_impl(repo_root, script_path, args)


def _spawn_once() -> None:
    """Best-effort spawn on the FileNotFoundError trigger, gated to at most
    one attempt per client process (Backstop 1). See module docstring's
    "Server-spawn seam". Idempotent: safe to call from more than one call
    site in this module without re-checking the guard at each site.

    Also gated on `_live_tree_cold`: a clone `engine_token()` has already
    identified as an unstamped live working tree cannot host a warm server
    (DR-315 s2, corroborated by DR-326/DR-331) -- spawning one is not merely
    wasted work, it is a doomed process every SessionStart would otherwise
    repeat. `engine_token()` is always called before this function on every
    reachable call path (`_try_warm_dispatch_inner` computes it before the
    pipe-open loop that calls `_spawn_once`), so `_live_tree_cold` is
    current by the time this check runs.

    W13: also consults `breadcrumb.should_spawn` before spawning -- the
    debounce check `warm_start.py` already wires into the SessionStart
    spawn trigger, but this is the OTHER path that can spawn (every CLI
    invocation whose pipe-open hits `FileNotFoundError`, one fresh process
    per concurrent caller). Backstop 1 above is per-PROCESS only, so on its
    own it does nothing to stop N concurrent clients from each winning
    their own one-per-process spawn race after an eviction; `should_spawn`
    is the cross-process debounce (a young, alive breadcrumb) that closes
    that race here too. Imported lazily, matching this module's own
    import-budget discipline (`spawn_detached`'s docstring) -- paid only on
    the `FileNotFoundError` branch that was already about to import
    `spawn_detached`'s own heavy `ops` chain, never on a warm hit or any
    other cold-fallback outcome."""
    global _spawned_this_process
    if _spawned_this_process:
        return
    if _live_tree_cold:
        return
    root = _engine_clone_root()
    from coordinator_core.warm.breadcrumb import should_spawn

    if not should_spawn(root):
        return
    _spawned_this_process = True
    spawn_detached(str(root), SERVER_ENTRY_SCRIPT)


#: How long a POSIX `connect()` may take before the server counts as
#: CONTENDED rather than absent. Not a latency budget and not fitted to an
#: observation: a listening `AF_UNIX` socket accepts in microseconds on the
#: local machine, so the only thing this bounds is a FULL BACKLOG, which is
#: the anti-storm table's "server up, contended -> go cold, never spawn"
#: row. The Windows arm answers that question instantly (`ERROR_PIPE_BUSY`
#: comes straight back from `open`); POSIX has no such immediate signal, so
#: a blocking `connect` against a full backlog would wait instead of going
#: cold -- and a wait is the one answer this seam must never give, since the
#: whole point of the cold fallback is that it is bounded.
CONNECT_DEADLINE_SECS = 0.25


def _endpoint_name(token: str) -> str:
    """This engine generation's endpoint: a named pipe on Windows, a unix
    socket path everywhere else.

    The socket arm asks `election.socket_path` rather than spelling the
    shape, for the same reason `server._elect_unix_socket_endpoint` does:
    it is the single production derivation, and the C door reproduces it
    independently. A second Python copy would let the two drift while both
    halves stayed green."""
    if sys.platform == "win32":
        return election.pipe_name(token)
    return str(election.socket_path(token))


def _open_pipe(endpoint: str):
    """Open the client end of the warm endpoint. Isolated as its own
    function so tests can monkeypatch the transport without a real named
    pipe or socket -- a seam four other modules name by this exact
    attribute, which is why it keeps the `_open_pipe` name now that it
    opens either transport.

    Windows: `open(pipe, "r+b")` -- the spike's measured API, no ctypes.

    POSIX: an `AF_UNIX` connect handed to `makefile("rwb")`, which is the
    SAME blocking `readline` / `write` / `flush` / `close` surface, so
    nothing downstream of this call is platform-aware. `sock.close()`
    immediately after `makefile` is the documented CPython idiom, not a
    bug, and is the accept side's own (`server._wrap_socket`): the file
    object holds its own reference to the fd, so the connection lives until
    the caller's single `fh.close()` in its `finally`. Without it every
    dispatch would leak a socket object.

    `settimeout(None)` before `makefile` is load-bearing: a socket left
    with a timeout gives a file object whose internal buffer is documented
    as inconsistent if one fires, and read deadlines are `_PendingRead`'s
    job on its own thread, never the socket's.

    `socket` is imported here rather than at module scope for this module's
    standing reason -- the Windows path must not pay an import it never
    reaches."""
    if sys.platform == "win32":
        return open(endpoint, "r+b")

    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_DEADLINE_SECS)
        sock.connect(endpoint)
        sock.settimeout(None)
        io = sock.makefile("rwb")
    except BaseException:
        sock.close()
        raise
    sock.close()
    return io


#: Sentinel distinguishing "the deadline expired" from "the server sent an
#: empty frame". The old `Optional[bytes]` return conflated them, which was
#: harmless while both went cold and is not once they diverge.
_TIMED_OUT = object()


class _PendingRead:
    """One `readline()` running in a daemon thread, waitable MORE THAN ONCE.

    Runs the blocking read off this thread so a wedged server never blocks
    this process past a deadline -- on final expiry the thread is simply
    abandoned (never joined again) and dies with the process.

    Waitable more than once because the two questions this module asks have
    different deadlines and only the FIRST is answerable up front: "is the
    server alive?" (`READ_DEADLINE_SECS`) and, when the answer to that is "no
    idea" for a mutation already delivered, "is it still working?"
    (`MUTATION_READ_DEADLINE_SECS`). Re-joining the same thread continues the
    SAME read -- re-issuing it would mean re-sending the request, which for a
    mutation is the very double-execution this class exists to prevent.
    """

    def __init__(self, fh: Any) -> None:
        self._result: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._run, args=(fh,), daemon=True)
        self._thread.start()

    def _run(self, fh: Any) -> None:
        try:
            self._result["line"] = fh.readline()
        except OSError as exc:
            self._result["exc"] = exc

    def wait(self, deadline_secs: float) -> Any:
        """The frame, or `_TIMED_OUT`. Re-raises an `OSError` the read itself
        hit (e.g. `BrokenPipeError` mid-response) so the caller's own
        broken-pipe handling covers it."""
        self._thread.join(deadline_secs)
        if self._thread.is_alive():
            return _TIMED_OUT
        if "exc" in self._result:
            raise self._result["exc"]
        return self._result.get("line")


def _op_may_mutate(method: Any) -> bool:
    """True unless the named op is affirmatively classified COMPUTE_ONLY.

    FAIL-CLOSED: an unknown op, an unimportable classification table, anything
    unexpected -- all answer True, because the cost of being wrong in that
    direction is one honest error, and the cost of being wrong in the other is
    a mutation executed twice.

    Imported lazily and ONLY from the post-delivery failure branches, never on
    the happy path: this module is on every invocation's cold-start preamble
    and is import-budget-guarded, and the classification table is neither
    small nor free.
    """
    try:
        from coordinator_core.authz.classification import OpClass, classify

        return classify(method) is not OpClass.COMPUTE_ONLY
    except Exception:  # noqa: BLE001 -- fail closed, see docstring
        return True


def _mutation_deadline_for(method: Any) -> float:
    """The mutation read deadline for `method`, derived from the same source
    of truth the CALLER's own kill ceiling comes from: `cc_invoke.py::
    _op_timeout_ceiling` sizes that ceiling as `engine_budget(op) + MARGIN`,
    and `engine_budget` is `ipc._timeout_for`. Deriving from the identical
    function keeps this wait inside the caller's ceiling for every mutating
    op, not just `ceremony.scoped_git_commit` -- see
    `MUTATION_READ_DEADLINE_SECS`'s own comment for the gap this closes.

    THE DERIVATION ALWAYS RUNS. An explicit `MUTATION_READ_DEADLINE_SECS`
    (a test, an operator) may only NARROW the result -- `min(override,
    derived)` -- never widen it and never replace it. A monkeypatch that
    lowers the deadline keeps working unchanged, which is what the existing
    tests pin; a reassignment that raises it is inert, because raising a
    dial to make a slow op fit is the habit the 2026-08-21 PM ruling bans,
    and because honoring one verbatim skipped this derivation outright and
    with it the `ceremony.*` `ipc.CEREMONY_BUDGET_SECS` clamp -- a clamp
    bypass reachable from any import-time reassignment of a module constant.

    Imported lazily and ONLY from the post-delivery expiry path, matching
    `_op_may_mutate`'s own lazy-on-failure-path-only pattern above: this
    module is on every invocation's cold-start preamble and is
    import-budget-guarded, and `ipc` is neither small nor free. On any
    import or lookup failure the derivation degrades to
    `_MUTATION_READ_DEADLINE_DEFAULT` -- the fail-safe floor, still subject
    to the same narrowing-only `min` -- never to an unbounded or unresolved
    wait.
    """
    try:
        from coordinator_core.ipc import _timeout_for

        derived = _timeout_for(method)
    except Exception:
        derived = _MUTATION_READ_DEADLINE_DEFAULT
    if MUTATION_READ_DEADLINE_SECS != _MUTATION_READ_DEADLINE_DEFAULT:
        return min(MUTATION_READ_DEADLINE_SECS, derived)
    return derived


_MUTATION_INDETERMINATE_MESSAGE = (
    "warm dispatch indeterminate: this MUTATING op's request was delivered to "
    "the warm engine, which did not answer in time. The op may have COMPLETED "
    "-- a slow op is not a hung one, and the engine does not stop when this "
    "client stops waiting. Reconcile against real state (e.g. `git log`) "
    "before re-running; re-running blind is how a duplicate commit happens. "
    "Deliberately NOT retried and NOT re-run cold here: a delivered mutation "
    "re-executed is exactly the double-execution this refusal prevents "
    "(state/bug-backlog/2026-08-19-scoped-git-commit-still-reports-a-landed-"
    "d4c7d9dc8e14.yaml)."
)


def _indeterminate_envelope(msg: dict, detail: str) -> dict:
    """A JSON-RPC error envelope for a delivered-but-unanswered mutation.

    Returned rather than raised, and returned rather than `None`, because both
    alternatives are wrong in the same direction: `None` is this module's
    "go cold" signal, which re-runs the op, and a raise is swallowed by
    `try_warm_dispatch`'s Backstop 2 into that same `None`. An envelope reaches
    `coordinator_core.invoke.__main__` as THE response, so the cold path is
    skipped and the caller's existing error ladder surfaces this text.
    """
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {
            "code": WARM_DISPATCH_INDETERMINATE,
            "message": f"{_MUTATION_INDETERMINATE_MESSAGE} ({detail})",
        },
    }


def try_warm_dispatch(msg: dict) -> Optional[dict]:
    """Attempt one warm-pipe request/response for JSON-RPC request `msg`.

    Returns the JSON-RPC response dict when the warm server served it --
    ANY well-formed response counts, including an error envelope, per the
    anti-storm table, EXCEPT an ENGINE_SKEW (-32002) error, which means
    "server up and stale" and goes cold instead. Returns None for every
    other outcome (warmth disabled, no pipe, busy, someone else's pipe, a
    broken mid-request pipe after one re-open, a malformed response, or
    read-deadline expiry) -- the caller (`coordinator_core.invoke.
    __main__.main`) falls through to its existing, unmodified cold
    dispatch path on None.

    Never raises -- Backstop 2, "the cold path is a SUCCESS path": nothing
    in this preamble may fail in a way that fails the op, so any
    unanticipated exception is caught here and treated as a cold-path
    signal rather than propagated.
    """
    try:
        result = _try_warm_dispatch_inner(msg)
    except Exception as exc:  # noqa: BLE001 -- Backstop 2: never fail the op
        print(
            f"[warm-client] preamble failed, falling back to cold: {exc!r}",
            file=sys.stderr,
        )
        _record_permanent_preamble_failure(exc)
        result = None
    if result is None:
        _record_cold_fallback(msg.get("method"))
    return result


def _record_permanent_preamble_failure(exc: Exception) -> None:
    """Record `_cold_reason` for a Backstop 2 failure that will recur
    identically on every retry, so the caller does not advise a retry.

    THE SECOND ROUTE TO ONE CONTRADICTION. `SocketPathTooLongError` is
    raised by `election.socket_path` when the runtime base makes the socket
    path exceed `sun_path` -- a property of the base, not of this call. Left
    unclassified it reached the caller as an ordinary transient miss, and
    the operator got the same "go cold, and cold is forbidden" pair that the
    absent-engine-root arm produced: warm unavailable, cold refused, retry
    advised, nothing that changes on a retry. The exception already carries
    both the path and the byte budget, so the reason wraps it verbatim
    rather than re-deriving either.

    Deliberately narrow. Every other Backstop 2 exception stays unclassified
    and keeps the retry advice: an unanticipated failure in the preamble is
    exactly the case where "this recurs forever" is a guess, and a wrong
    permanent verdict removes the advice that would have worked.

    NEVER OVERWRITES an already-established reason, and the asymmetry with
    `_log_live_tree_cold_once` (which always writes) is deliberate. Both
    conditions can hold at once -- an unstamped or absent engine root AND a
    runtime base too long to bind -- and the root-resolution reason is the
    one the operator must act on first, since it is upstream of which clone
    is even being asked to host a server. First permanent reason wins here;
    the root-resolution site outranks it and may replace it.
    """
    global _cold_reason
    if _cold_reason is not None:
        return
    try:
        from coordinator_core.warm.election import SocketPathTooLongError
    except Exception:
        return
    if isinstance(exc, SocketPathTooLongError):
        _cold_reason = (
            f"warm engine: {exc}. No server can bind that path, so no call from this "
            "runtime base reaches one. Remediation: set COORDINATOR_WARM_RUNTIME_BASE "
            "to a shorter directory."
        )


def _record_cold_fallback(op: "str | None" = None) -> None:
    """Record one cold fallback -- the instrument W6+W7 found dead: only
    THIS process (the client) ever observes a cold fallback, so this is
    the sole call site across the whole warm engine that can ever
    increment it. Lazily imported, matching `_op_may_mutate`'s own
    import-budget-guarded pattern: this module is on every invocation's
    cold-start preamble, and every `try_warm_dispatch` call that returns a
    served warm response must never pay this import. Never raises --
    `record_client_cold_fallback` is itself best-effort, and an import
    failure here is swallowed the same way (Backstop 2: nothing in the
    preamble may fail in a way that fails the op).

    `op` and the pid are carried so a BURST can be attributed. A row used to
    be a bare timestamp, and on 2026-08-25 this file recorded 1600 of them
    in 13 seconds (~123/s) with no way to say which process or which op
    produced any of them -- an unattributable defect report
    (state/bug-backlog/2026-08-26-sixteen-hundred-warm-misses-in-thirteen-
    seconds.yaml). Both values are already in hand here: `os` is imported
    inside this function rather than at module scope, matching this
    module's import budget (`is_warm_enabled`'s own note on why there is no
    top-level `import os`), and the cost lands only on a path that was
    already about to import `telemetry`."""
    try:
        import os

        from coordinator_core.warm.telemetry import record_client_cold_fallback

        record_client_cold_fallback(engine_root=_engine_clone_root(), op=op, pid=os.getpid())
    except Exception:  # noqa: BLE001 -- Backstop 2, see docstring
        return


def _try_warm_dispatch_inner(msg: dict) -> Optional[dict]:
    if not is_warm_enabled():
        return None

    # P2 (docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md § C3):
    # `engine_token()` was called TWICE per dispatch -- once to build the
    # pipe name, once to stamp the request -- paying `skew.compute_client_token()`
    # (two file stats) a second time for a value that cannot have changed
    # between the two call sites within one preamble invocation. Computed
    # once and reused below.
    token = engine_token()
    pipe = _endpoint_name(token)
    request = {**msg, "_engine_token": token}
    caller_sid = _caller_session_id()
    if caller_sid:
        request["_session_id"] = caller_sid
    # Publish-lane seam (DR-350), the same shape and the same reason as `_session_id`
    # directly above: a long-lived warm server's `os.environ` reflects whoever SPAWNED
    # it, never the caller of any given request, so a fact about THIS caller has to
    # cross the pipe explicitly rather than be re-resolved server-side. Stamped only
    # when this client process is itself inside a declared percolate/publish round;
    # absent otherwise, never sent as `false`, so a server reads absence exactly as it
    # reads an older client -- not in the lane.
    #
    # Imported inside the function, not at module scope, because this module holds a
    # strict import budget: it sits on every invocation's cold-start preamble and does
    # not even carry a top-level `import os` (see `is_warm_enabled`). `publish_lane` is
    # stdlib-only and its parent package is necessarily already loaded to have reached
    # this module at all, so the cost after the first call is a `sys.modules` lookup.
    from coordinator_core import publish_lane

    if publish_lane.env_declares_lane():
        request[publish_lane.PUBLISH_LANE_FIELD] = True
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"

    # At most 2 attempts: the original open, plus the table's single
    # "BrokenPipeError mid-request -> one re-open" retry. Anything else
    # returns (cold) without looping.
    for attempt in range(2):
        try:
            fh = _open_pipe(pipe)
        except (FileNotFoundError, ConnectionRefusedError):
            # ECONNREFUSED joins ENOENT because on POSIX the two are one
            # outcome wearing two shapes: "no server". A unix socket's path
            # OUTLIVES the process that bound it, so a hard-killed server
            # leaves a corpse file that exists and refuses -- the same
            # condition a missing named pipe reports as ENOENT on Windows.
            # Reclaiming that corpse is the ELECTION's job under its flock
            # (`election.reclaim_stale_socket`), never this client's: a
            # client that unlinked on refusal would race a peer's live
            # socket. Spawning is the correct and sufficient response, and
            # `should_spawn` still rate-limits it.
            _spawn_once()
            return None
        except PermissionError:
            return None
        except TimeoutError:
            # `CONNECT_DEADLINE_SECS` expired -- the POSIX counterpart of
            # ERROR_PIPE_BUSY below. A listening socket accepts immediately,
            # so this is a full backlog: server up, contended. Go cold,
            # never spawn (spawning here is the storm the table forbids).
            # Raised by `settimeout`, which sets NO errno, so it cannot be
            # classified by the `exc.errno` tests below.
            return None
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_PIPE_BUSY:
                return None
            if exc.errno in _CONTENDED_ERRNOS:
                # The same contended-server outcome reached without the
                # deadline firing: a non-blocking-ish refusal from the
                # accept queue (EAGAIN), or a connection the kernel set up
                # and then dropped for backlog pressure (ECONNABORTED,
                # ECONNRESET). Cold, never spawn -- same row as the two
                # branches above.
                return None
            if exc.errno == errno.EINVAL:
                # A second, distinct contended-pipe outcome -- see the
                # module docstring's anti-storm table row for this branch
                # (problem 4 of the 2026-08-19 concurrency problem set).
                # Never a named winerror==231 case (that branch above would
                # already have caught it), but empirically just as much
                # "server up, contended" as ERROR_PIPE_BUSY is -- go cold,
                # never spawn, and never fall through to Backstop 2's
                # generic stderr write for a classified outcome.
                return None
            raise

        # THE INVARIANT THIS BLOCK EXISTS TO HOLD: once a MUTATING request has
        # been DELIVERED, this client never re-sends it and never goes cold.
        # Every "go cold" exit below re-runs the op in a fresh cold spawn, and
        # every `continue` re-sends it -- both of which are correct only while
        # the server cannot have executed the request. After a successful
        # flush that is no longer true, and treating it as true is the
        # 2026-08-19 defect: a `git commit` outran the 2s liveness deadline,
        # the client went cold, the cold engine committed nothing (the paths
        # were already committed by the warm server, still finishing), and the
        # operator was told "no commit landed" about a commit that exists --
        # under a DIFFERENT Commit-Token, because the second execution minted
        # its own. Evidence: peer session 30008a4b, e527554b8/b330d767d.
        #
        # `delivered` is set only after `flush()` returns: a write that fails
        # part-way leaves a partial frame, which the server cannot parse as a
        # request and therefore never dispatches -- safe to re-open.
        delivered = False
        try:
            fh.write(payload)
            fh.flush()
            delivered = True
            pending = _PendingRead(fh)
            line = pending.wait(READ_DEADLINE_SECS)
            if line is _TIMED_OUT:
                # The liveness probe expired. For a compute-only op that is the
                # table's own "wedged server -> go cold" row, unchanged. For a
                # mutation it asks the wrong question: the server is not wedged,
                # it is WORKING. Keep waiting on the SAME read (never a second
                # request) up to the mutation's own deadline.
                if not _op_may_mutate(msg.get("method")):
                    return None
                mutation_deadline = _mutation_deadline_for(msg.get("method"))
                line = pending.wait(
                    max(0.0, mutation_deadline - READ_DEADLINE_SECS)
                )
                if line is _TIMED_OUT:
                    return _indeterminate_envelope(
                        msg, f"no response within {mutation_deadline}s"
                    )
        except BrokenPipeError:
            if delivered and _op_may_mutate(msg.get("method")):
                return _indeterminate_envelope(msg, "pipe broke after delivery")
            if attempt == 0:
                continue  # the table's one re-open
            return None
        finally:
            try:
                fh.close()
            except OSError:
                pass

        # From here on the server ANSWERED -- bytes came back -- but the answer
        # is unusable. Bytes back means it read and dispatched the request, so
        # for a mutation the work may well have been performed and re-running
        # it cold could double-execute. These collapse onto the honest refusal.
        # The zero-byte case is deliberately NOT one of them; see its own
        # branch below for the evidence that separates them.
        def _cold_or_indeterminate(detail: str):
            if _op_may_mutate(msg.get("method")):
                return _indeterminate_envelope(msg, detail)
            return None

        if not line or not line.strip():
            # EOF with NOT ONE BYTE back. This is the one post-delivery shape
            # that goes cold even for a mutation, and the distinction is
            # evidence, not convenience: every other shape here has the server
            # demonstrably ENGAGED with this request (it answered slowly, or it
            # answered with something), whereas a zero-byte close is what a
            # server that never serviced the connection at all looks like.
            # That is the common case on this box, not a corner: the warm
            # server keeps a single pending listener
            # (docs/problems/2026-08-19-the-warm-engine-serves-one-caller-at-a-
            # t.md), so under the stated load norm a connection routinely dies
            # unserviced. `flush()` returning proves the bytes left THIS
            # process into the pipe buffer -- it never proved the server read
            # them, and treating it as proof is what made a large-pathspec
            # commit fail outright here instead of taking the cold path it has
            # always taken (caught by
            # `test_large_pathspec_round_trips_end_to_end`).
            #
            # Never-worse check, which is what settles it: a server that read,
            # dispatched, and died before answering ALSO produces this shape,
            # and for that one going cold can double-execute. But that was
            # equally true before this change -- cold is what it already did --
            # so this branch is the pre-existing behaviour preserved, not a
            # hole this change opens. The defect this change exists to close
            # (a WORKING server outrunning a 2s liveness probe) lands on the
            # deadline branch above, which stays strict.
            return None

        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            return _cold_or_indeterminate("malformed response frame")
        if not isinstance(response, dict) or "jsonrpc" not in response:
            return _cold_or_indeterminate("response is not a JSON-RPC envelope")

        # Re-emit the op's diagnostics on THIS process's stderr, which is where
        # a cold spawn would have put them and therefore the only stream the
        # caller reads. Popped, not passed through: nothing downstream may see
        # a response shape that differs between warm and cold. See
        # `warm.entry_seam`'s DIAGNOSTIC-axis note.
        op_stderr = response.pop("_stderr", None)
        if isinstance(op_stderr, str) and op_stderr.strip():
            print(op_stderr, file=sys.stderr, flush=True)

        error = response.get("error")
        if isinstance(error, dict) and error.get("code") == ENGINE_SKEW:
            # The ONE post-delivery cold fallback that stays safe for a
            # mutation, and it is safe by the server's own construction, not by
            # assumption: `server.py::_serve_one` performs its skew check and
            # returns BEFORE it calls `dispatch(msg)`. A skewed server has
            # provably not executed the op, so re-running it cold cannot
            # duplicate anything.
            return None
        return response

    return None


# ---------------------------------------------------------------------------
# AC3: a stdlib-only, `-S -E`-runnable CLI fast path.
#
# Everything above this line is `try_warm_dispatch` -- the in-process API
# `invoke/__main__.py` and `scripts/setup.py` call today, imported as
# `coordinator_core.warm.client`. That import path pays whatever
# `sys.path`/`site` machinery got this process to `coordinator_core` in the
# first place; it is not itself `-S -E`-runnable, because `-S -E` skips site
# initialization and this file's own directory is not the repo root, so
# `import coordinator_core...` cannot resolve under those flags.
#
# `_cli_main` below is the OTHER shape AC3 asks for: this file run DIRECTLY
# (`python -S -E coordinator_core/warm/client.py '<json-request>'`), never
# `import`ed as part of `coordinator_core`. Executing a `.py` file as a
# script does not import its parent packages -- only an explicit
# `import coordinator_core....` statement would, and this section is
# deliberately written to never issue one. Every name `_cli_main` needs
# (pipe name, warm-enabled check, engine token) is a small, INLINED
# duplicate of the equivalent logic in `election.py` / `settings.py` /
# `skew.py`, not a call into them -- that duplication is the point: it is
# what makes `python -S -E client.py` avoid `coordinator_core`'s own
# `__init__.py` chain (and, transitively, `machine_resolver`'s
# `_settings_home` import) entirely, at the cost of tracking those three
# modules by hand if their logic changes.
#
# NEGATIVE SPEC -- this fast path only honors the `COORDINATOR_WARM` env-var
# rung of `settings.is_warm_enabled`'s three-rung precedence (module
# docstring, rung 1), not the `engine.warm.enabled` machine-local TOML
# registry rung (rung 2): reading that registry without importing
# `coordinator_core.machine_resolver` would mean re-deriving
# `registry_dir()`'s settings-home resolution ladder by hand, in a form that
# silently drifts from the real one the next time that ladder changes. A
# machine that opted into warmth ONLY via the registry key (no
# `COORDINATOR_WARM` env var set) is UNROUTED by this fast path and falls to
# `False` -- i.e. this path degrades to "warmth disabled", never to a wrong
# answer, matching `is_warm_enabled()`'s own unset-default. Not general
# routing (that is C4's job for the fired path); this is the CLI-scoped
# artifact AC3 asks for.
#
# Never called from `try_warm_dispatch` or `_try_warm_dispatch_inner` above,
# and nothing above this line calls into it -- the two paths are siblings,
# not layers.
# ---------------------------------------------------------------------------

CLI_TRUTHY = frozenset({"1", "true", "yes", "on"})
CLI_FALSY = frozenset({"0", "false", "no", "off"})


def _cli_is_warm_enabled() -> bool:
    """`COORDINATOR_WARM`-only re-derivation of `settings.is_warm_enabled`'s
    rung 1 -- see this section's own negative-spec above for why rung 2 (the
    TOML registry) is deliberately not consulted here."""
    import os

    value = os.environ.get("COORDINATOR_WARM", "").strip().lower()
    return value in CLI_TRUTHY


def _cli_user_sid() -> str:
    """Re-derivation of `election.current_user_sid()`, stdlib `ctypes` only
    -- see `election.py` for the annotated original this mirrors."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    token_query = 0x0008
    token_user = 1

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    htoken = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(htoken)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(htoken, token_user, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            htoken, token_user, buf, size.value, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        str_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str_sid.value
        finally:
            kernel32.LocalFree(str_sid)
    finally:
        kernel32.CloseHandle(htoken)


def _cli_engine_token(repo_root) -> str:
    """Re-derivation of `skew.compute_client_token`'s LIVE-WORKING-TREE
    branch only (stat pair of `.git/HEAD` and the ref it names) -- the
    PUBLISHED-tree `_engine_stamp` branch is not duplicated here because
    reading it needs no more than a `read_bytes()`, so this fast path reads
    it the same way `skew.py` does, inline, rather than skip it."""
    import hashlib

    root = Path(repo_root)
    stamp = root / "coordinator_core" / "_engine_stamp"
    try:
        stamp_bytes = stamp.read_bytes()
    except OSError:
        stamp_bytes = None
    if stamp_bytes is not None:
        return hashlib.sha1(b"engine-stamp:" + stamp_bytes).hexdigest()[:16]

    git_dir = root / ".git"

    def _stat_pair(p: Path):
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, 0)

    head = git_dir / "HEAD"
    ref_path = None
    try:
        content = head.read_text(encoding="utf-8").strip()
        if content.startswith("ref:"):
            ref_path = git_dir / content[len("ref:"):].strip()
    except OSError:
        pass
    signal = (_stat_pair(head), _stat_pair(ref_path) if ref_path is not None else (0, 0))
    return hashlib.sha1(repr(signal).encode("utf-8")).hexdigest()[:16]


def _cli_pipe_name(repo_root) -> str:
    """Re-derivation of `election.pipe_name` -- see that function's own
    docstring for the pipe-name shape this reproduces."""
    import hashlib

    clone_hash = hashlib.sha1(str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:16]
    sid = _cli_user_sid()
    token = _cli_engine_token(repo_root)
    return f"\\\\.\\pipe\\coordinator-core.{sid}.{clone_hash}.{token}"


def _cli_main(argv) -> int:
    """Entry point when this file is run directly (`python -S -E
    coordinator_core/warm/client.py '<json-request>'`). Reads the JSON-RPC
    request from argv[0] (or stdin if argv is empty), attempts ONE pipe
    round trip with no retry and no server-spawn (this fast path never
    spawns -- see the module docstring's anti-storm table; a
    `FileNotFoundError` here is simply "no warm server", printed as `null`),
    and prints the response (or `null` on any non-response outcome) as
    compact JSON to stdout. Never raises: any unanticipated exception is
    caught, `null` is printed, and 0 is still returned -- this fast path
    exists to be cheap, not to replace `try_warm_dispatch`'s full anti-storm
    and fail-open discipline, which stays the in-process API's job."""
    try:
        raw = argv[0] if argv else sys.stdin.buffer.read().decode("utf-8")
        msg = json.loads(raw)

        if not _cli_is_warm_enabled():
            print("null")
            return 0

        repo_root = _engine_clone_root()
        pipe = _cli_pipe_name(repo_root)
        payload = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"

        try:
            fh = open(pipe, "r+b")
        except OSError:
            print("null")
            return 0
        try:
            fh.write(payload)
            fh.flush()
            line = fh.readline()
        finally:
            try:
                fh.close()
            except OSError:
                pass

        if not line or not line.strip():
            print("null")
            return 0
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            print("null")
            return 0
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except Exception:  # noqa: BLE001 -- never fail the caller, see docstring
        print("null")
        return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main(sys.argv[1:]))
