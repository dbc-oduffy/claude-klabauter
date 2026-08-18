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

import json
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
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


def _read_line_with_deadline(fh, deadline_secs: float) -> Optional[bytes]:
    """Read one newline-terminated frame from `fh`, abandoning the read if
    it has not completed within `deadline_secs` (the "read-deadline
    expiry" table row). Runs the blocking `readline()` in a daemon thread
    so a wedged server never blocks this process past the deadline -- on
    expiry that thread is simply abandoned (never joined again), and dies
    with the process. Returns None on expiry, the same signal every other
    "go cold" exit of this module uses. Re-raises an `OSError` the read
    itself hit (e.g. `BrokenPipeError` mid-response) so the caller's
    existing broken-pipe handling covers it too.
    """
    result: dict[str, Any] = {}

    def _read() -> None:
        try:
            result["line"] = fh.readline()
        except OSError as exc:
            result["exc"] = exc

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(deadline_secs)
    if reader.is_alive():
        return None
    if "exc" in result:
        raise result["exc"]
    return result.get("line")


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
            raise

        try:
            fh.write(payload)
            fh.flush()
            line = _read_line_with_deadline(fh, READ_DEADLINE_SECS)
        except BrokenPipeError:
            if attempt == 0:
                continue  # the table's one re-open
            return None
        finally:
            try:
                fh.close()
            except OSError:
                pass

        if not line or not line.strip():
            return None

        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(response, dict) or "jsonrpc" not in response:
            return None

        error = response.get("error")
        if isinstance(error, dict) and error.get("code") == ENGINE_SKEW:
            return None
        return response

    return None
