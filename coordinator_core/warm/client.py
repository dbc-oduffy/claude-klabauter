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
    ENGINE_SKEW (-32001)             -- server up and stale: go cold.
    read-deadline expiry             -- go cold.
    Backstop 1: ONE spawn attempt per client process, ever.
    Backstop 2: the cold path is a SUCCESS path -- nothing in this
        preamble may fail in a way that fails the op.

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
"""

from __future__ import annotations

import errno
import json
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from coordinator_core.warm import election
from coordinator_core.warm.settings import is_warm_enabled

__all__ = [
    "ERROR_PIPE_BUSY",
    "ENGINE_SKEW",
    "READ_DEADLINE_SECS",
    "SERVER_ENTRY_SCRIPT",
    "engine_token",
    "try_warm_dispatch",
]

ERROR_PIPE_BUSY = 231
ENGINE_SKEW = -32001
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
#: past that ceiling for every mutating op except `ceremony.scoped_git_commit`
#: (the only entry in `ipc.py::_OP_TIMEOUT_OVERRIDES`, at 150s): every other
#: op resolves to `ipc.DISPATCH_TIMEOUT_SECS` = 30s, giving the caller a ~40s
#: ceiling, so this client was killed by its own parent long before a flat
#: 120s wait could ever return the `WARM_DISPATCH_INDETERMINATE` envelope --
#: the exact protection this deadline exists to guarantee, silently absent
#: everywhere it was not the incident's own op (WARN, coordinator-code-reviewer
#: sidecar dcf219af, SLICE 1). `_mutation_deadline_for` below derives the real
#: wait per-op from `ipc._timeout_for` -- the same source of truth the
#: caller's own ceiling is derived from -- so the wait stays inside that
#: ceiling for every op, not just the one this incident concerned. This
#: constant is now only the fail-safe floor: 30.0 mirrors `ipc`'s own
#: `DISPATCH_TIMEOUT_SECS` default, so it can never itself outlive an
#: un-derivable op's caller ceiling.
MUTATION_READ_DEADLINE_SECS = 30.0

#: Frozen at import time, never reassigned -- the yardstick `_mutation_deadline_for`
#: compares `MUTATION_READ_DEADLINE_SECS` against to tell "nobody touched the
#: constant, derive per-op" from "something (a test, an operator) explicitly
#: overrode it, honor that override verbatim." Without this, a monkeypatched
#: `MUTATION_READ_DEADLINE_SECS` would be silently ignored once the per-op
#: derivation is in place -- it is not consulted at all when derivation
#: succeeds.
_MUTATION_READ_DEADLINE_DEFAULT = MUTATION_READ_DEADLINE_SECS

#: JSON-RPC error code for "your mutation was delivered and its outcome is
#: unknown". Distinct from INTERNAL_ERROR so a caller can tell "the op failed"
#: from "the op may well have SUCCEEDED and we cannot see the answer".
WARM_DISPATCH_INDETERMINATE = -32004
SERVER_ENTRY_SCRIPT = "coordinator_core/warm/server.py"

_spawned_this_process = False


def _engine_clone_root() -> Path:
    """This coordinator_core clone's own resolved root -- the same
    computation `election._default_engine_clone` makes for `pipe_name`'s
    own default, kept as a local copy rather than reaching into that
    function's private name. Used as the `repo_root` `spawn_detached` logs
    housekeeping failures under."""
    return Path(__file__).resolve().parents[2]


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

    Import direction is client -> skew, never the reverse: `skew` duplicates
    `ENGINE_SKEW` rather than importing this module precisely to keep that
    edge acyclic (`skew.test_engine_skew_constant_matches_client_module`
    pins the two constants equal).

    Falls back to the previous stable placeholder if the token cannot be
    computed. A token that raises would fail the preamble, and nothing in
    the preamble may fail in a way that fails the op (C15's contract); a
    constant token merely means skew eviction cannot fire, which degrades
    to today's behaviour rather than breaking the call.

    Spec backlink: plan 2026-08-16-one-engine-for-the-whole-box, C15 seam
    filled by C16.
    """
    try:
        from coordinator_core.warm.skew import compute_client_token

        return compute_client_token()
    except Exception:
        return "unversioned"


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
    site in this module without re-checking the guard at each site."""
    global _spawned_this_process
    if _spawned_this_process:
        return
    _spawned_this_process = True
    spawn_detached(str(_engine_clone_root()), SERVER_ENTRY_SCRIPT)


def _open_pipe(pipe: str):
    """Open the client end of the warm pipe -- the spike's measured API,
    no ctypes. Isolated as its own function so tests can monkeypatch the
    transport without a real named pipe."""
    return open(pipe, "r+b")


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

    Honors an explicit override first: if `MUTATION_READ_DEADLINE_SECS` no
    longer equals `_MUTATION_READ_DEADLINE_DEFAULT`, something (a test, an
    operator) deliberately set it, and that value wins verbatim -- this is
    also what keeps the existing monkeypatch-based tests pinning real
    behaviour rather than requiring them to reach into `ipc` as well.

    Imported lazily and ONLY from the post-delivery expiry path, matching
    `_op_may_mutate`'s own lazy-on-failure-path-only pattern above: this
    module is on every invocation's cold-start preamble and is
    import-budget-guarded, and `ipc` is neither small nor free. Falls back
    to `MUTATION_READ_DEADLINE_SECS` on any import or lookup failure --
    fail safe toward the honest floor, never an unbounded or unresolved
    wait.
    """
    if MUTATION_READ_DEADLINE_SECS != _MUTATION_READ_DEADLINE_DEFAULT:
        return MUTATION_READ_DEADLINE_SECS
    try:
        from coordinator_core.ipc import _timeout_for

        return _timeout_for(method)
    except Exception:
        return MUTATION_READ_DEADLINE_SECS


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
    anti-storm table, EXCEPT an ENGINE_SKEW (-32001) error, which means
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
        return _try_warm_dispatch_inner(msg)
    except Exception as exc:  # noqa: BLE001 -- Backstop 2: never fail the op
        print(
            f"[warm-client] preamble failed, falling back to cold: {exc!r}",
            file=sys.stderr,
        )
        return None


def _try_warm_dispatch_inner(msg: dict) -> Optional[dict]:
    if not is_warm_enabled():
        return None

    pipe = election.pipe_name(engine_token())
    request = {**msg, "_engine_token": engine_token()}
    caller_sid = _caller_session_id()
    if caller_sid:
        request["_session_id"] = caller_sid
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"

    # At most 2 attempts: the original open, plus the table's single
    # "BrokenPipeError mid-request -> one re-open" retry. Anything else
    # returns (cold) without looping.
    for attempt in range(2):
        try:
            fh = _open_pipe(pipe)
        except FileNotFoundError:
            _spawn_once()
            return None
        except PermissionError:
            return None
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_PIPE_BUSY:
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
