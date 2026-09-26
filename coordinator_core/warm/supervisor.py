"""coordinator_core.warm.supervisor — the http route's supervisor guarantee.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
§ C11, AC10b. `disposition: conditional` -- this module exists BECAUSE C8
selected `http` (its own sidecar, `state/subagent-share/c152238e/
2026-08-19-the-fired-path-reaches-the-engine.C8.md`: "Decision: `http`, not
the cheap-client repoint"). R1 binds hardest here: `http` is taken WITH this
guarantee or not taken at all -- no advisory-hooks-only exemption, no
partial supervisor (C11's own chunk body).

WHAT THIS MODULE OWNS, per the Director of Engineering's enumeration (C11's chunk body) --
    - a supervised resident listener process (`main()` / `_ServerContext`);
    - autostart -- `ensure_listener()` spawns one without a human starting
      it, exactly like `warm.client._spawn_once`'s trigger shape, reusing
      `coordinator_core.ops.ceremony.detached_spawn.spawn_detached`;
    - health checking -- `check_health()`, a real GET against a `/health`
      endpoint, never assumed from a discovery record alone;
    - port discovery -- `write_discovery` / `read_discovery`: a fired hook
      (or `ensure_listener`, standing in for one until a caller wires the
      hook script itself) learns the bound port from a per-clone,
      per-user runtime file, never a hardcoded port that would collide
      across 50-70 concurrent sessions;
    - per-machine election -- `main()` reuses `warm.election.elect()`'s
      kernel-atomic first-instance-pipe mechanism, under a DISTINCT pipe
      name (the `"http."`-prefixed token below) so this election can never
      collide with the pipe-transport server's own (`warm.server.main`)
      election on the identical clone;
    - fail-open parity (P12) -- `ensure_listener()` never waits for a
      listener to BOOT (mirrors `warm.client`'s "NO CLIENT EVER WAITS
      FOR A SERVER TO BOOT"; it does wait up to `HEALTH_CHECK_TIMEOUT_SECS`
      on a live-pid-but-hung listener, see that function's own docstring)
      and returns `None` on every failure mode
      (no discovery record, a dead pid, a failed health check, a spawn
      that hasn't bound a port yet). `None` is this module's whole
      fail-open contract: a caller sees "no reachable http listener this
      call" and falls back to its own already-existing local path --
      never a hang, never a raised exception. C10's probe already proved
      an unreachable http endpoint fails open at the HARNESS layer
      (`docs/research/warm-engine-premise/c10-http-probe.md`, Q3); this
      module is what makes "unreachable" an ACTIONABLE, checked state on
      the claude-klabauter side rather than an assumption.
    - skew self-eviction -- C9 (AC18/AC19): `main()`'s skew-only watchdog
      thread (`_ServerContext._skew_watchdog_loop`) retires a listener
      whose `engine_token` a publish has rotated past, without waiting for
      a caller to contact it -- see `_ServerContext`'s own docstring.

WHAT THIS MODULE MUST NOT REIMPLEMENT -- every one of these already exists,
mirroring `warm.server`'s own negative-spec section for the pipe transport:
  - election -> `warm.election.elect()` / `pipe_name()`. This module picks
    a distinct engine_token namespace, never a second locking primitive.
  - generation token -> `warm.skew.compute_client_token()`.
  - spawn -> `coordinator_core.ops.ceremony.detached_spawn.spawn_detached`,
    the SAME lazy-imported wrapper `warm.client.spawn_detached` uses (import
    cost is paid only on the actual spawn trigger, never on every call).
  - shutdown -> `warm.lifecycle`'s single ordered, single-shot sequence.
  - in-flight accounting -> `warm.server.InFlightCounter`, reused rather
    than a second counter class.

WHAT THIS MODULE DELIBERATELY DOES NOT DO (negative-spec, WIRING NOTE
convention per `warm.breadcrumb`'s own docstring) --
  - Does NOT translate a real hook payload (`PreToolUse` JSON body) into a
    `permissionDecision` -- the `/hook` handler below is a present,
    resident, health-checked SEAM (echoes the request id, answers "allow"
    by default) rather than a business-logic gap; wiring real hook
    semantics into it, and pointing `hooks.json`'s `type: "http"` entries
    at the discovered port, are BOTH follow-up chunks, outside this row's
    `writes:` (`coordinator_core/warm/supervisor.py` and its test only).
  - Does NOT write into `warm.breadcrumb`'s own `warm.json` -- that file is
    the PIPE server's breadcrumb; writing a second shape into it would
    corrupt `breadcrumb.should_spawn`'s pid/epoch comparison for a
    completely different process. This module keeps its OWN discovery file
    (`DISCOVERY_FILENAME`) in the same per-clone `svc_dir()`, reusing that
    resolution helper (a pure read, not a shared mutable file) without
    touching `breadcrumb.py`'s writes: at all.
  - Does NOT wait for a listener to BOOT -- see fail-open bullet above for
    the health-probe wait it does pay.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root

from coordinator_core import locked_write
from coordinator_core.session.core import stable_pid_alive
from coordinator_core.warm import (
    breadcrumb,
    cookie,
    election,
    hook_http,
    lifecycle,
    skew,
    telemetry,
)
from coordinator_core.warm.http_listener import (
    ENGINE_TOKEN_HEADER,
    _collect_response,
    _frame_from_request,
)
from coordinator_core.warm.server import InFlightCounter, _declare_execution_route, _serve_line

__all__ = [
    "record_is_skewed",
    "DISCOVERY_FILENAME",
    "HEALTH_PATH",
    "HOOK_PATH",
    "HEALTH_CHECK_TIMEOUT_SECS",
    "SPAWN_DEBOUNCE_SECS",
    "ENTRY_SCRIPT",
    "discovery_path",
    "write_discovery",
    "read_discovery",
    "read_discovery_with_cause",
    "diagnose_no_backend",
    "CAUSE_RECORD_PRESENT",
    "CAUSE_RECORD_ABSENT",
    "CAUSE_RECORD_UNREADABLE",
    "CAUSE_RECORD_UNPARSEABLE",
    "CAUSE_RECORD_MALFORMED",
    "READ_CAUSES",
    "unlink_discovery",
    "discovery_is_live",
    "should_spawn",
    "listener_url",
    "check_health",
    "supervisor_pipe_name",
    "supervisor_lock_path",
    "ensure_listener",
    "main",
]

DISCOVERY_FILENAME = "warm-http.json"

HEALTH_PATH = "/health"

HOOK_PATH = hook_http.HOOK_PATH

# The METHOD_NOT_FOUND path this constant used to describe is not dead, only no longer
GUARD_OP_NAME = hook_http.DEFAULT_OP_NAME

# READ_DEADLINE_SECS`'s "is the server wedged" framing, sized the same
HEALTH_CHECK_TIMEOUT_SECS = 2.0

# Reuses `breadcrumb.SPAWN_DEBOUNCE_SECS` rather than defining a second
SPAWN_DEBOUNCE_SECS = breadcrumb.SPAWN_DEBOUNCE_SECS

# `warm.server._IDLE_WATCHDOG_POLL_SECS` -- both ask "has this generation
_SKEW_WATCHDOG_POLL_SECS = 5.0

# file itself (mirrors `warm.client.SERVER_ENTRY_SCRIPT` / `warm.server`'s
ENTRY_SCRIPT = "coordinator_core/warm/supervisor.py"


def _default_engine_clone() -> Path:
    return current_engine_clone()


def discovery_path(engine_root: Optional[Path] = None) -> Path:
    return breadcrumb.svc_dir(engine_root) / DISCOVERY_FILENAME


_replace_with_retry = locked_write.replace_with_retry


def write_discovery(
    *,
    port: int,
    pid: int,
    stable_pid_start_epoch: int,
    engine_sha: Optional[str],
    started_at: Optional[str] = None,
    engine_root: Optional[Path] = None,
) -> None:
    if started_at is None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    record = {
        "port": port,
        "pid": pid,
        "stable_pid_start_epoch": stable_pid_start_epoch,
        "engine_sha": engine_sha,
        "started_at": started_at,
        "health_path": HEALTH_PATH,
        "hook_path": HOOK_PATH,
    }
    path = discovery_path(engine_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    # ATOMIC REPLACE, NOT TRUNCATE-THEN-WRITE. The lock below serialises
    # MEASURED, not theorised (doe-claude-5a's sink, 2026-08-25, n=445): two
    # healthy neighbours -- thirty-odd MICROSECONDS, three orders of magnitude
    # mkstemp in the TARGET'S OWN DIRECTORY so `os.replace` is a same-volume
    with locked_write.held_lock(path, holder_label="warm.supervisor"):
        payload = json.dumps(record, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".discovery-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if not _replace_with_retry(tmp_path, str(path)):
                # Contended past the budget. The in-place fallback is a TRUNCATING write --
                # CORRECTED 2026-08-25, on evidence. The original wrote in place
                #     `_serve_line` answers ENGINE_SKEW: LOUD, and it evicts, so the next fire
                if not path.exists():
                    path.write_text(payload, encoding="utf-8", newline="\n")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def read_discovery(engine_root: Optional[Path] = None) -> Optional[dict]:
    return read_discovery_with_cause(engine_root)[0]


CAUSE_RECORD_PRESENT = breadcrumb.CAUSE_RECORD_PRESENT
CAUSE_RECORD_ABSENT = breadcrumb.CAUSE_RECORD_ABSENT
CAUSE_RECORD_UNREADABLE = breadcrumb.CAUSE_RECORD_UNREADABLE
CAUSE_RECORD_UNPARSEABLE = breadcrumb.CAUSE_RECORD_UNPARSEABLE
CAUSE_RECORD_MALFORMED = breadcrumb.CAUSE_RECORD_MALFORMED
READ_CAUSES = breadcrumb.READ_CAUSES


def read_discovery_with_cause(
    engine_root: Optional[Path] = None,
) -> "tuple[Optional[dict], str]":
    """`read_discovery`, plus why when there is no record -- one of
    `READ_CAUSES`. Thin wrapper over `breadcrumb.read_record_with_cause`,
    which owns the body and the retry policy for both HTTP transports; this
    supplies only the path, because the path is the only thing that differs.
    """
    return breadcrumb.read_record_with_cause(discovery_path(engine_root))


def diagnose_no_backend(
    engine_root: Optional[Path] = None,
    *,
    path_resolver: "Optional[Callable[..., Path]]" = None,
) -> dict:
    """Answer "why is there no reachable warm backend RIGHT NOW", cheaply, for a
    caller that has already failed to reach one.

    THE ASK THIS DISCHARGES, verbatim from DoE (2026-09-01): *"`no_backend`
    collapses at least six distinct causes into one counter, which is exactly why
    the incident could not diagnose itself from its own dial file. If the engine
    side has any way to tell me WHICH of those fired, that is the highest-value
    thing you could hand me next."* Their counter is theirs; this is the engine-side
    half they cannot compute -- the state of the record, and the identity of the
    directory it was looked for in.

    Returns a plain dict, never an object, because the intended consumer serialises
    it into a degrade row on a stdlib-only path where `coordinator_core` may be
    unimportable -- see that caller's own reasoning for why its durability
    mechanism must not share a failure mode with the thing it records.

    Keys, all always present:

    - `cause`: one of `READ_CAUSES`. `record_present` means THE ENGINE SIDE IS NOT
      THE PROBLEM -- a well-formed record exists, and a caller still seeing no
      backend is looking at a connect-side failure (its own `unreachable` arm),
      not an absent listener.
    - `discovery_path` / `svc_dir`: the file actually consulted, and its directory.
      **This is the field the 38-minute incident most needed and nobody had.** A
      record is per-clone and per-user (`breadcrumb.svc_dir`), so a caller that
      resolves a different `engine_root` than the running listener reads a
      different file and sees a permanent, self-consistent "absent" while the
      listener is healthy on the port its own record names. That state is
      indistinguishable from a dead engine through any counter, and visible
      immediately by comparing this path against the listener's.
    - `engine_root`: the root that produced the path above, so the divergence has
      a name and not just a symptom.
    - `record`: the parsed record when `cause` is `record_present`, else None.
      Callers get the port/pid/endpoint without a second read.

    NEGATIVE-SPEC. This function does NOT connect, probe, spawn, stat a pid, or
    call `check_health` -- it is one read of a file the caller's own failure path
    already touched, so it stays affordable on a degraded hot path where every
    session on the box may be arriving at once. It therefore CANNOT tell you the
    listener is dead; it tells you what the record says and where it looked, which
    is the half that was missing. A caller wanting liveness has `check_health`
    already and must pay for it deliberately.

    `path_resolver` selects WHICH transport's record to explain, defaulting to
    this module's. `front_door.discovery_path` is the other one -- passed rather
    than duplicated, so a front door that takes the 47623 seat under the
    succession contract inherits this diagnosis instead of re-growing the
    collapse under a new owner (DoE, 2026-09-01: "on the day it does, that gap
    becomes exactly this bug again with a different owner").

    Never raises -- an instrument for explaining a degraded state may not add a
    second failure to it. An unresolvable engine root reports itself as a cause
    with the paths absent, rather than propagating.
    """
    resolve = path_resolver or discovery_path
    try:
        path = resolve(engine_root)
        record, cause = breadcrumb.read_record_with_cause(path)
        return {
            "cause": cause,
            "discovery_path": str(path),
            "svc_dir": str(path.parent),
            "engine_root": str(engine_root) if engine_root is not None else str(_default_engine_clone()),
            "record": record,
        }
    except Exception as exc:  # noqa: BLE001 -- see docstring; never a second failure
        return {
            "cause": "engine_root_unresolvable",
            "discovery_path": None,
            "svc_dir": None,
            "engine_root": None,
            "record": None,
            "detail": "%s: %s" % (type(exc).__name__, exc),
        }


def unlink_discovery(
    engine_root: Optional[Path] = None,
    *,
    owner_pid: Optional[int] = None,
) -> None:
    """Best-effort remove the discovery file -- mirrors `breadcrumb.
    unlink_breadcrumb`'s never-raises contract. The owner-checked branch
    below additionally swallows `locked_write.LockTimeout`: contention on
    `held_lock` past its own timeout is reachable at this repo's stated
    50-70 concurrent-session load norm, and letting it escape would abort
    `_ServerContext.ctx_shutdown` before `_release_election_handle` runs,
    permanently leaking the won election handle. Never raises, full stop.

    `owner_pid` makes the unlink OWNERSHIP-CHECKED, exactly as
    `breadcrumb.unlink_breadcrumb` and `election.unlink_if_owned` already
    are: the file is removed only if the record on disk still names that
    pid. There is exactly ONE discovery file per clone and every winning
    listener overwrites it at boot, so a departing listener is NOT
    necessarily the listener the current record describes -- a superseded
    generation exiting would otherwise delete its LIVE SUCCESSOR's record:

        A boots (pid 1111)          -> discovery names 1111
        publish; B boots (pid 2222) -> discovery names 2222, A's clobbered
        A exits, unconditional unlink -> discovery GONE while B still serves

    That is not hypothetical here. Measured 2026-08-30 on this box: up to 9
    concurrent HTTP listeners, 92 of 131 lifetimes serving zero requests,
    and death groups of 4-8 processes within one second -- every one of
    those exits deleting the surviving listener's record. The next
    `http_hook_forwarder._resolve_backend` then reads `None` and DENIES a
    PreToolUse call the guard never evaluated.

    `owner_pid=None` keeps the historical unconditional behaviour for
    callers that genuinely own the file unambiguously; prefer passing it.
    """
    path = discovery_path(engine_root)
    if owner_pid is not None:
        # Held across READ-THEN-UNLINK, closing the TOCTOU `4a6aeac9ed` left:
        # this call then deletes the SUCCESSOR's fresh record rather than
        try:
            with locked_write.held_lock(path, holder_label="warm.supervisor"):
                record = read_discovery(engine_root)
                if record is None:
                    return
                if record.get("pid") != owner_pid:
                    return
                try:
                    path.unlink()
                except OSError:
                    pass
        except locked_write.LockTimeout:
            pass
        return
    try:
        path.unlink()
    except OSError:
        pass


def discovery_is_live(record: dict) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    stored_epoch = record.get("stable_pid_start_epoch")
    stored_epoch_str = str(stored_epoch) if stored_epoch is not None else ""
    try:
        return stable_pid_alive(pid, stored_start_epoch=stored_epoch_str)
    except Exception:
        return False


def should_spawn(engine_root: Optional[Path] = None, *, now: Optional[float] = None) -> bool:
    """The debounce decision for THIS module's own discovery file --
    delegates to `breadcrumb.should_spawn_decision`, this package's one
    shared decision body (see that function's docstring for the full
    contract).

    RETIRED RATIONALE, stated rather than left to go stale: this docstring
    used to read "duplicated rather than parameterized into that function
    because `breadcrumb.py` sits outside this chunk's `writes:`" -- true of
    the chunk that wrote it, and no longer true of this one. C4's own
    `writes:` names `supervisor.py`, `breadcrumb.py`, and `front_door.py`
    together, which is exactly the condition that rationale named as
    missing. See `breadcrumb.should_spawn_decision`'s docstring for the
    review finding (Kira #4) this retirement answers.

    True iff nothing currently vouches for an in-flight listener boot --
    including, as of this chunk, the case where NO record exists at all: a
    boot-in-flight lock (`breadcrumb.try_claim_boot`) is now consulted for
    that case rather than handing back an unconditional `True`, which is
    exactly the succession-window hole `docs/problems/2026-09-01-forwarder-
    no-backend-denies.md`'s measurement traces this multiplier to.
    """
    record = read_discovery(engine_root)
    return breadcrumb.should_spawn_decision(
        record,
        now=now,
        lock_path=breadcrumb.boot_lock_path(discovery_path(engine_root)),
    )


def listener_url(record: dict) -> Optional[str]:
    port = record.get("port")
    if not isinstance(port, int):
        return None
    return f"http://127.0.0.1:{port}"


def _pinned_hosts(port: int) -> frozenset:
    """The exact `Host` values this listener answers on, for `port`.

    DERIVED FROM `listener_url` ON PURPOSE -- the authority this listener
    publishes and the authority it accepts are one decision, and two
    spellings of it would let a client dial a URL the pin then refuses.
    `localhost` is included as the equivalent spelling of the same
    loopback endpoint.

    LITERALS ONLY. Never a prefix/suffix test and never a "looks like an
    IP" heuristic: webpack-dev-server's CVE-2025-30360 accepted any
    IP-literal as local, which an attacker's own IP satisfies. A value
    like `127.0.0.1:<port>.evil.com` must NOT match, which set membership
    gives for free and a substring test would not.

    IPv4 LOOPBACK ONLY, because the listener itself binds only IPv4
    (`("127.0.0.1", 0)`) -- `[::1]:<port>` cannot reach the handler to be
    compared. Anything that moves that bind to dual-stack must add the
    v6 literal here in the same change, or the pin refuses the very
    authority `listener_url` would then publish.
    """
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def check_health(
    url: str,
    *,
    timeout: float = HEALTH_CHECK_TIMEOUT_SECS,
    opener: Any = None,
) -> bool:
    """GET `url + HEALTH_PATH`; True iff it answers with a 2xx status
    within `timeout`. ANY failure -- connection refused, timeout, a
    non-2xx status, a malformed URL -- is `False`, never raised: a health
    check that could itself raise would defeat the whole point of a
    liveness probe an unreachable-endpoint caller must be able to trust.

    `opener` is an injectable `urllib.request.urlopen`-shaped callable for
    tests, defaulting to the real one -- mirrors `warm.client._open_pipe`'s
    own isolated-transport-seam convention.
    """
    import urllib.error
    import urllib.request

    open_url = opener if opener is not None else urllib.request.urlopen
    try:
        with open_url(url.rstrip("/") + HEALTH_PATH, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return 200 <= int(status) < 300
    except Exception:  # noqa: BLE001 -- a health check must never raise, see docstring
        return False


def supervisor_pipe_name(
    engine_root: Optional[Path] = None,
    *,
    user_sid: Optional[str] = None,
) -> str:
    root = engine_root if engine_root is not None else _default_engine_clone()
    token = skew.compute_client_token(root)
    return election.pipe_name(f"http.{token}", engine_clone=root, user_sid=user_sid)


def supervisor_lock_path(engine_root: Optional[Path] = None) -> Path:
    """The POSIX per-machine election's lock path -- the exclusion this
    module wins on a platform with no named pipes.

    `<svc dir>/http.<engine token>`, whose `.lock` sidecar
    (`election.elect_exclusive_lock`) is the file actually locked. Same
    per-clone, per-user directory the discovery record lives in, and the same
    `"http."` token prefix `supervisor_pipe_name` uses, for the same reason:
    `warm.server`'s own POSIX election binds `<svc dir>/<token>.sock` from an
    UNPREFIXED token, so the prefix is what keeps the two transports'
    exclusions from colliding on one clone.

    Not a socket path and never bound -- this election is a pure LOCK here
    (see `main`), so nothing dials this name and it is deliberately not run
    through `election.socket_path`'s `sun_path` budget.
    """
    root = engine_root if engine_root is not None else _default_engine_clone()
    token = skew.compute_client_token(root)
    return breadcrumb.svc_dir(root) / f"http.{token}"


def _elect_supervisor_slot(engine_root: Path) -> Any:
    """Win this clone's single-supervisor slot and return the handle that
    holds it -- or raise `election.ElectionLost`, which carries the
    contested endpoint, if a peer already holds it.

    ONE ELECTION, TWO PRIMITIVES, mirroring `warm.server._run_guarded`'s own
    `_elect_windows_pipe` / `_elect_unix_socket_endpoint` split. Windows takes
    a named pipe's FIRST INSTANCE; POSIX takes the `flock`
    `election.elect_exclusive_lock` wraps. Neither handle is ever served on:
    unlike `warm.server`, this module's transport is the TCP port `main`
    binds separately, so the election here is exclusion and nothing else --
    which is why the POSIX arm is a bare lock rather than a second socket.

    WHY THIS EXISTS AT ALL, measured 2026-09-02: `main` used to call
    `election.current_user_sid()` unconditionally, which raises
    `RuntimeError("current_user_sid is Windows-only")` on every POSIX box.
    Every supervisor spawned by `ensure_listener` therefore died in step 1,
    before binding and before writing a discovery record -- silently, because
    `spawn_detached` DEVNULLs the child's stdio and never reads its exit
    code, so the only symptom was `ensure_listener` returning None forever
    and every hot-path invocation falling through to cold. Nothing caught it
    because every test of this election is `skipif(sys.platform != "win32")`.
    """
    if sys.platform == "win32":
        sid = election.current_user_sid()
        name = supervisor_pipe_name(engine_root, user_sid=sid)
        return election.elect(name, user_sid=sid)

    return election.elect_exclusive_lock(supervisor_lock_path(engine_root))


def record_is_skewed(record: dict, root: Path) -> bool:
    """PUBLIC alias of `_record_is_skewed`, for a caller that reads discovery
    DIRECTLY rather than through `ensure_listener`.

    WHY THIS EXISTS, and it closes a real hole rather than tidying a name.
    `read_discovery` runs no skew check by design -- it is a lock-free hot-path
    read and callers want the record as written. The skew predicate below is
    reached from exactly ONE place, `ensure_listener`. So a consumer that reads
    discovery itself and dials the URL it finds has no sanctioned way to ask
    "is this record skewed?", and every such consumer gets the failure the
    predicate exists to prevent:

        `read_discovery` returns a skewed record (not None, parses fine)
        -> the caller's own no-backend trigger is gated on `record is None`
        -> so it never calls `ensure_listener`, so this predicate never runs
        -> the listener IS alive and reachable, so no error arm fires either
        -> the POST lands, `_serve_line` answers ENGINE_SKEW (-32002)
        -> the guard does not run, and nothing denies

    That is `DoE-claude http_hook_forwarder.py`'s live shape, traced by
    `doe-claude-b4` 2026-08-26. The forwarder's own "no backend is a trigger,
    not just a verdict" doctrine never engages, because the backend DID answer
    -- with a non-verdict, relayed verbatim, allow included.

    Publishing the predicate is claude-klabauter's half of the fix: a direct reader can
    now ask the question without duplicating skew logic and without depending
    on `ensure_listener`'s side effects. The other half is the caller's, and it
    is a permission decision on their surface, not ours: a `-32002` answer must
    be treated as NO BACKEND and denied, exactly like the unreachable arm.
    This function decides nothing and denies nothing -- it only makes the state
    askable.
    """
    return _record_is_skewed(record, root)


def _record_is_skewed(record: dict, root: Path) -> bool:
    """True when a discovery record advertises an `engine_sha` that is not the
    one this clone would compute now -- i.e. the engine was republished under a
    listener that is still running and still answering `GET /health`.

    WHY `ensure_listener` MUST NOT HAND SUCH A LISTENER OUT. `discovery_is_live`
    and `check_health` both PASS for a skewed listener: the process is alive and
    `/health` returns 200, because health never traverses `_serve_line` and so
    never reaches the version check. The fire then POSTs it, `_serve_line`
    answers ENGINE_SKEW (-32002), and the guard DOES NOT RUN -- observed
    2026-08-25 against a listener 7h04m stale, and again on a registered
    `type: "http"` hook twenty minutes later, both times with the model told
    only that the guard "errored out".

    Returning True here sends the caller down its ordinary cold path instead,
    where the guard RUNS. That is the whole of the fix: a skew costs a warm hit,
    never a skipped guard. The stale listener is left to `evict_on_skew`, which
    retires it on the first contact from any current-token caller.

    Negative-spec: this is NOT the publish-side eviction that the front-door
    plan's C9 owns. Nothing here restarts a listener or shortens its life; it
    only stops a stale one being reported as usable. C9 still has a job -- the
    orphaned listener holds its port until something contacts it -- and this
    change makes that job a tidy-up rather than a correctness gate.

    Never raises: an unreadable stamp or a record with no `engine_sha` returns
    False, preserving the pre-existing behaviour for anything it cannot judge
    rather than declining a listener on a failure to establish skew.
    """
    advertised = record.get("engine_sha")
    if not advertised:
        return False
    try:
        return skew.compute_client_token(root) != advertised
    except Exception:  # noqa: BLE001 -- fail-open parity: cannot establish is not skewed
        return False


def ensure_listener(engine_root: Optional[Path] = None, *, now: Optional[float] = None) -> Optional[str]:
    """The autostart + health-check + port-discovery + fail-open entry
    point AC10b names: returns a live listener's base URL, or `None` if
    none is reachable THIS call.

    NEVER WAITS FOR A BOOT -- mirrors `warm.client`'s "NO CLIENT EVER WAITS
    FOR A SERVER TO BOOT" doctrine, for the identical reason: with idle
    demotion (this package's `warm.idle`), "no listener yet" is the
    ordinary first call after any quiet period, not a rare cold start.

    IT DOES WAIT UP TO `HEALTH_CHECK_TIMEOUT_SECS`, corrected 2026-08-26.
    The line above read a bare "NEVER WAITS" until this subsystem's
    succession investigation checked it against the body: branch 1 calls
    `check_health`, a SYNCHRONOUS `urllib.request.urlopen` bounded by
    `HEALTH_CHECK_TIMEOUT_SECS` (2.0s). A discovery record naming a live
    pid whose HTTP listener has hung therefore costs this call the full
    timeout. That is not a boot wait, but it is a wait, and it is paid on
    `warm/server.py :: _run_guarded`'s own boot path -- between the op
    registry preload and `serve_forever` -- so it lands on the successor's
    time-to-answerable. See
    `docs/research/2026-08-26-repo-warm-succession.md` § 4 and the
    advisory's item 5, which proposes moving the call off that path rather
    than shortening the timeout.

    1. A live, healthy discovery record -> its URL.
    2. Otherwise, if nothing currently vouches for an in-flight boot
       (`should_spawn`), best-effort spawn one and return `None` THIS call
       -- the caller falls back to its own existing local path, exactly as
       `warm.client._spawn_once` triggers a pipe-server spawn and still
       goes cold the same call.
    3. Any other outcome (a young in-flight boot already vouched for, a
       spawn that has not yet bound a port) also returns `None` -- fail
       open, no wait, no exception.

    GATED ON A STAMPED ENGINE ROOT, BEFORE ANY OF THE THREE. An unstamped
    tree is not an engine (`docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md`),
    and `_compute_engine_token` already refuses one -- so a listener spawned
    against it could only ever answer `_serve_line`'s untrusted-caller
    refusal. Spawning it anyway is pure litter, and it is litter on the
    OPERATOR'S REAL MACHINE: `svc_dir()` keys off the real `%LOCALAPPDATA%`,
    not a test's HOME-only quarantine, so an ungated call from any test that
    does not mock this function spawns a real detached process per run. The
    gate lives HERE, not at each call site, so that every caller inherits it
    rather than each one remembering -- `warm/entry_seam.py :: _trigger_listener_boot`
    keeps its own copy only to avoid IMPORTING this module on the Bash hot
    path, which is a different job.

    Never raises: every read/health-check primitive it calls already has
    a "never raises, degrade to None/False" contract, and this function
    adds no unguarded call of its own.
    """
    root = engine_root if engine_root is not None else _default_engine_clone()
    if not is_engine_root(root):
        return None
    try:
        record = read_discovery(root)
        if record is not None and discovery_is_live(record) and not _record_is_skewed(record, root):
            url = listener_url(record)
            if url is not None and check_health(url):
                return url

        if should_spawn(root, now=now):
            spawn_detached(str(root), ENTRY_SCRIPT)
        return None
    except Exception:  # noqa: BLE001 -- fail-open parity (P12): never fail the caller
        return None


def spawn_detached(repo_root: str, script_path: str, args: Optional[Any] = None) -> bool:
    """Lazy delegate to `ops.ceremony.detached_spawn.spawn_detached` --
    THE IMPORT IS INSIDE THE FUNCTION on purpose, mirroring `warm.client.
    spawn_detached`'s own docstring: importing `coordinator_core.ops`
    registers the entire ~316-module op surface, and this module's own
    read/health-check path (the overwhelming majority of `ensure_listener`
    calls) must never pay that cost. Module-level name, not inlined at the
    call site, so a test can monkeypatch it the same way `warm.client`'s
    tests patch `client.spawn_detached`.
    """
    from coordinator_core.ops.ceremony.detached_spawn import (
        spawn_detached as _spawn_detached_impl,
    )

    return _spawn_detached_impl(repo_root, script_path, args)


def _self_stable_pid_start_epoch() -> Optional[int]:
    from coordinator_core.session.core import _win_create_time_epoch

    try:
        return _win_create_time_epoch(os.getpid())
    except Exception:
        return None


def _release_election_handle(handle: Optional[Any]) -> None:
    if handle is None:
        return
    if sys.platform != "win32":
        election.release_exclusive_lock(handle)
        return

    import _winapi

    try:
        _winapi.CloseHandle(handle)
    except Exception:  # noqa: BLE001 -- best-effort close of a won lock
        pass


class _ServerContext:
    """Boot-scoped supervisor state: the in-flight counter every request
    handler shares, and the shutdown wiring `warm.lifecycle` needs. Mirrors
    `warm.server._ServerContext`'s shape at the scale this module actually
    needs. CORRECTED 2026-08-25 -- the two clauses this docstring used to
    join were not both true, and a sibling repo's published retraction rests
    on telling them apart.

    NO IDLE WATCHDOG -- still true, and load-bearing. This module never
    imports `warm.idle` and `main` runs `serve_forever()` until killed:
    nothing in this process demotes the listener for idleness. (A `/hook`
    POST DOES reach `idle.mark_invocation`, since `_serve_line` takes it as
    a default argument and `serve_kwargs` below does not override it -- but
    the marks are inert here, because no watchdog reads them. `GET /health`
    never traverses `_serve_line` at all.) So an availability dip on this
    transport is never explained by idle demotion.

    NO SKEW EVICTION -- FALSE as previously written, and measured false.
    This class does not IMPLEMENT eviction, but it wires it: `serve_kwargs`
    hands `_serve_line` this context's `close_listener` and `drain`, which
    are precisely the two callables `skew.evict_on_skew` takes, and every
    `/hook` POST routes through `_serve_line`. A fresh-token request against
    a stale listener therefore evicts it -- observed 2026-08-25 against a
    listener 7h04m stale, retired on first contact. Eviction is INHERITED
    from `_serve_line`, not absent.

    C9 (AC18/AC19) CLOSES THE REMAINING GAP: `ensure_listener` is reached
    only from the cold hook path (`entry_seam`, `server`), never from
    publish, so with no traffic a stale listener used to drift indefinitely
    while `GET /health` answered 200 -- measured live 2026-08-25, 7h04m
    stale. `main()` now starts a SECOND, skew-only watchdog thread
    (`_skew_watchdog_loop` / `_skew_watchdog_tick` / `_token_is_stale`,
    mirroring `warm.server._ServerContext`'s own idle-watchdog SHAPE at the
    scale this transport needs) that polls `skew.compute_client_token`
    against this context's own `engine_token` every
    `_SKEW_WATCHDOG_POLL_SECS` and calls `self.stop()` (the same
    `lifecycle.begin_shutdown` wiring below) the first time they disagree --
    turning "held until something contacts it" into "held for at most one
    poll interval past the publish that rotated the stamp." This is NOT the
    idle-demotion watchdog `warm.server` runs: it reads no served-count, no
    seconds-idle, and fires on staleness alone -- the module docstring's "no
    idle watchdog" bullet still holds for THIS transport in the idle sense.
    """

    def __init__(
        self,
        *,
        httpd: Any,
        engine_root: Optional[Path],
        version_state: "skew.ServerVersionState",
        dispatch: Optional[Any] = None,
        election_handle: Optional[Any] = None,
    ) -> None:
        self.httpd = httpd
        self.engine_root = engine_root
        self._election_handle = election_handle
        self.in_flight = InFlightCounter()
        # THIS TRANSPORT HAD NO TELEMETRY AT ALL until 2026-08-26, so every
        self.engine_token = self._compute_engine_token()
        self.telemetry = telemetry.ServerTelemetry(
            transport="http", engine_token=self.engine_token
        )
        self.version_state = version_state
        self.server_sha = version_state.server_sha
        # `GUARD_OP_NAME` names (`warm_guard.evaluate`, `ops/warm_guard_evaluate.py`) so
        self.dispatch = dispatch
        self._skew_watchdog_stop = threading.Event()

    def _compute_engine_token(self) -> Optional[str]:
        """This transport's own trust proof, SELF-STAMPED rather than read off a
        caller-supplied header -- `hooks.json`'s `type: "http"` caller (Claude Code) has
        no notion of `_engine_token` and sends none. Supervisor knows its own engine
        root, so it computes the identical token `_serve_line`'s version-skew check
        expects (`skew.compute_client_token`, the SAME primary token a named-pipe
        client stamps) and places it into every frame itself, rather than inventing a
        second scheme beside `_serve_line`'s existing one (module docstring's
        negative-spec: no second auth scheme). `None` on any failure to resolve it (an
        unstamped clone) degrades every `/hook` POST to `_serve_line`'s own
        untrusted-caller refusal -- loud, never a crash, never a silent allow.
        """
        try:
            return skew.compute_client_token(self.engine_root)
        except Exception:  # noqa: BLE001 -- fail-open parity: a bad stamp must not crash boot
            return None

    def record_invocation(self, warm: bool) -> None:
        self.telemetry.record_invocation(warm=warm)

    def record_exit(self, reason: str, detail: Optional[str] = None) -> None:
        self.telemetry.record_exit(reason, detail)

    def drain(self) -> None:
        return None

    def close_listener(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001 -- best-effort, mirrors ctx_shutdown's own contract
            pass

    def ctx_shutdown(self) -> None:
        self._skew_watchdog_stop.set()
        # `unlink_discovery` are each best-effort/never-raises BY CONTRACT,
        try:
            self.telemetry.flush(engine_root=self.engine_root)
            unlink_discovery(self.engine_root, owner_pid=os.getpid())
        finally:
            _release_election_handle(self._election_handle)
            self._election_handle = None

    def stop(self) -> None:
        lifecycle.begin_shutdown(
            close_listener=self.close_listener,
            in_flight_count=self.in_flight,
            ctx_shutdown=self.ctx_shutdown,
        )

    def _token_is_stale(self) -> bool:
        if self.engine_token is None:
            return False
        try:
            return skew.compute_client_token(self.engine_root) != self.engine_token
        except Exception:  # noqa: BLE001 -- fail-open parity: a read failure is not a verdict
            return False

    def _skew_watchdog_tick(self) -> None:
        """One watchdog poll: self-evict via `stop()` (this class's own
        `lifecycle.begin_shutdown` wiring, shared with every other trigger's
        single-shot guard) the first time `_token_is_stale()` is True. A
        no-op otherwise.

        RECORDS THE REASON BEFORE STOPPING, mirroring `warm.server::
        _ServerContext._idle_tick`, which records before demoting for the
        same reason: `stop()` -> `lifecycle.begin_shutdown` -> `ctx_shutdown`
        flushes telemetry, so a reason set after `stop()` is never written.
        Without this call every HTTP-transport skew eviction landed as
        `exit_reason: null` -- 115 of 540 recorded lifetimes (21%) measured
        2026-08-30, an entire census bucket reading as "unknown" when it was
        this one path. Residual of `docs/research/2026-08-26-repo-warm-
        succession-advisory.md` section 6, whose `ServerTelemetry` and
        `transport` tag landed while this call did not."""
        if self._token_is_stale():
            self.record_exit(telemetry.EXIT_REASON_SUPERSEDED)
            self.stop()

    def _skew_watchdog_loop(self) -> None:
        while not self._skew_watchdog_stop.wait(_SKEW_WATCHDOG_POLL_SECS):
            self._skew_watchdog_tick()


def _make_handler(ctx: "_ServerContext"):
    from http.server import BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def parse_request(self) -> bool:
            """ONE central Host check, ahead of stdlib's dispatch to any
            `do_*` method -- deliberately here and not in each handler.

            WHY `parse_request` AND NOT `handle_one_request`: stdlib parses
            the request line and headers INSIDE `parse_request`, so a check
            placed in `handle_one_request` ahead of the `super()` call sees
            no `self.headers` at all and silently passes everything -- a pin
            that is not a pin. `handle_one_request` calls
            `if not self.parse_request(): return`, so returning False here
            halts before dispatch, which is exactly the semantics wanted.

            WHY HOST, AND WHY NOT AN ORIGIN ALLOWLIST. Origin validation is
            a PER-HANDLER discipline: it has to be re-applied on every route
            and every upgrade path, and it lapses silently the moment
            someone adds one. That is not hypothetical -- Vite shipped
            Origin checks on its HTTP path and not on its WebSocket upgrade
            TWICE (CVE-2025-24010, CVE-2026-39363), and in both cases the
            check was never RUN, so its logic was irrelevant. `Host` is
            present on every HTTP request line, WebSocket handshakes
            included, so it can be validated ONCE, before routing, and
            cannot lapse as the code grows. If a WS/SSE upgrade path is
            ever added here, its Origin check belongs in THIS method, not
            beside the new route.

            LITERAL COMPARISON ONLY -- never a prefix, suffix, or
            "looks like an IP" test. webpack-dev-server's CVE-2025-30360
            accepted any IP-literal as local, which an attacker's own IP
            satisfies.

            This is defence-in-depth against browser-borne requests, NOT
            authentication: it stops a page the operator visits, and stops
            a DNS-rebound name (rebinding changes where the socket lands,
            never the `Host` the browser writes). The credential is a
            separate, load-bearing control.
            """
            if not super().parse_request():
                return False
            if not self._host_is_pinned():
                self.close_connection = True
                try:
                    self.send_error(421, "Misdirected Request")
                except Exception:  # noqa: BLE001 -- never fail the listener on a refusal
                    pass
                return False
            if not self._cookie_is_valid():
                self.close_connection = True
                try:
                    self.send_error(401, "Unauthorized")
                except Exception:  # noqa: BLE001 -- never fail the listener on a refusal
                    pass
                return False
            return True

        def _refuse_stale_caller(self, caller_token: str) -> bool:
            """True iff this caller was refused (and the response written).

            § Refusal semantics row 2. THE AXIS DISTINCTION THE COMPARISON
            CANNOT MAKE: `ServerVersionState.is_skewed` is a plain
            inequality, so a token mismatch reads identically whether the
            CALLER is behind or this SERVER is stranded. On the named pipe
            that ambiguity is harmless -- the token is part of the pipe
            name, so a stale caller dials a name that does not exist and
            goes cold. A fixed published port has no such binding, so the
            transport has to supply the distinction, and this is it.

            Server current + caller behind (axis 1) -> REFUSE THIS CALLER
            with `ENGINE_SKEW` so it retries cold. Never
            `close_listener`/`drain`: the server is fine, the caller is
            behind, and evicting would take the warm engine down for every
            session on the box -- measured at 16.8s under a 17s drain.

            Server itself stale (axis 2) -> return False and let the
            request through to `_serve_line`, whose eviction is CORRECT
            there and is deliberately left unchanged: that axis is the
            server judging itself stale, which is true regardless of who
            asked.

            Fail-open on an unreadable live token, deliberately: if the
            live stamp cannot be computed we cannot know the caller is
            behind, and refusing every caller on a stamp-read failure
            would be an outage of its own. The cookie gate upstream is the
            control that fails closed.
            """
            if self._token_is_stale_server_side():
                return False
            try:
                live = skew.compute_client_token(ctx.engine_root)
            except Exception:  # noqa: BLE001 -- see the docstring's fail-open note
                return False
            # GENERATION STAMP, not a bearer secret. Nothing is granted by
            if caller_token == live:
                return False
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": skew.ENGINE_SKEW,
                        "message": (
                            "engine generation changed; recompute "
                            "skew.compute_client_token() and retry cold"
                        ),
                    },
                }
            ).encode("utf-8")
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        def _token_is_stale_server_side(self) -> bool:
            try:
                return bool(ctx._token_is_stale())
            except Exception:  # noqa: BLE001 -- see below
                return False

        def _cookie_is_valid(self) -> bool:
            """True iff this request carries the boot cookie.

            THE LOAD-BEARING CONTROL. The `Host` pin above is
            defence-in-depth against a browser; this is what actually
            establishes that the caller can read a file only this user
            can read.

            Placed in `parse_request` for the same reason the Host pin is:
            once, before routing, where it cannot lapse as `do_*` methods
            are added. It is INDEPENDENT of the skew refusal in
            `do_POST` -- that one answers WHICH ENGINE GENERATION the
            caller thinks it is dialling and is coupled to the
            self-stamping line; this one answers WHO IS CALLING. Same spec
            section, different question, different site.

            HEALTH IS EXEMPT, DELIBERATELY. `GET /health` returns the
            fixed literal `ok` and reveals nothing an open port does not
            already reveal, while `check_health` is the probe callers run
            BEFORE they have any reason to have read the cookie -- gating
            it would break discovery to protect nothing. The exemption is
            this narrow: one path, one method, a fixed body.

            FAIL CLOSED ON THE SERVER'S OWN SIDE TOO. If the expected
            cookie cannot be read at request time -- deleted, corrupted,
            or permissions changed under a running listener -- every
            caller is refused rather than admitted. Boot already refuses
            these cases (`_assert_credential_ready`); this covers the
            window after boot.
            """
            if self.command == "GET" and self.path.rstrip("/") == HEALTH_PATH:
                return True
            expected = cookie.read(ctx.engine_root)
            if not expected:
                return False
            headers = getattr(self, "headers", None)
            if headers is None:
                return False
            sent = headers.get_all(cookie.COOKIE_HEADER) or []
            if len(sent) != 1:
                return False
            return hmac.compare_digest(sent[0].strip(), expected)

        def _host_is_pinned(self) -> bool:
            """True iff this request's `Host` is one of the loopback
            literals this listener actually publishes.

            Called from `parse_request` AFTER `super().parse_request()`
            has populated `self.headers` -- the `headers is None` guard
            below is belt-and-braces for a subclass or stdlib change that
            reorders that, not a live path.

            The accepted set is EXACTLY what this listener's own
            `listener_url` builds (`127.0.0.1:<bound port>`) plus the
            equivalent `localhost` spelling. `localhost` is safe to accept
            for the same reason `Host` works at all: an attacker cannot
            make a browser send someone else's name.

            Compared CASE-INSENSITIVELY against the (lower-case) pinned
            set: hostnames are case-insensitive per RFC 9110, so a
            `Host: LOCALHOST:<port>` that this listener genuinely published
            must not read as a foreign authority.
            """
            headers = getattr(self, "headers", None)
            if headers is None:
                return True
            sent = headers.get_all("Host") or []
            if len(sent) > 1:
                return False
            host = (sent[0] if sent else "").strip().lower()
            if not host:
                return self.request_version == "HTTP/1.0"
            return host in _pinned_hosts(int(self.server.server_address[1]))

        def do_GET(self) -> None:  # noqa: N802 -- stdlib-mandated name
            if self.path.rstrip("/") == HEALTH_PATH:
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def _respond_json(self, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 -- stdlib-mandated name
            op_name = hook_http.op_for_path(self.path)
            if op_name is None:
                self.send_response(404)
                self.end_headers()
                return

            ctx.in_flight.enter()
            released = False

            def _release_once() -> None:
                nonlocal released
                if not released:
                    released = True
                    ctx.in_flight.exit()

            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    event = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    event = {}
                if not isinstance(event, dict):
                    event = {}

                event_name = event.get("hook_event_name")

                # the bare-/hook safety check because it resolves to the same DEFAULT_OP_NAME
                if op_name == hook_http.DEFAULT_OP_NAME:
                    if hook_http.route_for_event(event_name) is None:
                        self._respond_json(hook_http.unserved_response(event_name))
                        return

                # THE CALLER'S ENVIRONMENT ARRIVES IN HEADERS, NOT IN THE BODY, and this
                header_env, disarm_reason = hook_http.env_from_headers(self.headers)
                if disarm_reason is not None:
                    # A DECLARED-BUT-VETOED CHANNEL IS AN UNRUN GUARD, NOT A CLEAN ONE. The
                    self._respond_json(
                        hook_http.unreachable_response(
                            event_name or "PreToolUse", disarm_reason
                        )
                    )
                    return
                if header_env:
                    event = {**event, "env": header_env}

                request_frame = hook_http.build_request(event, op_name)
                # THE CALLER'S TOKEN IS CHECKED HERE AND NEVER FORWARDED.
                # instead of `ENGINE_SKEW`. `_refuse_stale_caller` is that
                # A TOKENLESS request is unchanged: nothing to check, and the
                caller_token = self.headers.get(ENGINE_TOKEN_HEADER)
                if caller_token is not None and self._refuse_stale_caller(caller_token):
                    _release_once()
                    return
                request_frame = _frame_from_request(request_frame, ctx.engine_token)

                serve_kwargs = {
                    "version_state": ctx.version_state,
                    "server_sha": ctx.server_sha,
                    "close_listener": ctx.close_listener,
                    "drain": ctx.drain,
                    "release_in_flight": _release_once,
                    "record_invocation": ctx.record_invocation,
                    "record_exit": ctx.record_exit,
                }
                if ctx.dispatch is not None:
                    serve_kwargs["dispatch"] = ctx.dispatch

                raw_response = _collect_response(request_frame, _serve_line, serve_kwargs)
                if _is_engine_skew(raw_response):
                    # PROVABLY NOT RUN, AND RUNNABLE COLD. `_serve_line` answers
                    # ENGINE_SKEW without dispatching, so the guard can still be
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw_response)))
                    self.end_headers()
                    self.wfile.write(raw_response)
                    return
                response = hook_http.interpret_result(event_name, raw_response)

                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                _release_once()

    return _Handler


def _is_engine_skew(frame: bytes) -> bool:
    """True iff `frame` is a JSON-RPC error envelope carrying ENGINE_SKEW."""
    try:
        obj = json.loads(frame.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    err = obj.get("error") if isinstance(obj, dict) else None
    return isinstance(err, dict) and err.get("code") == skew.ENGINE_SKEW


def _assert_credential_ready(root: Path) -> None:
    """Generate the cookie if absent, then assert the directory holding it
    excludes other users. Raises `cookie.DirectoryNotPrivateError` if it
    does not (AC2), or `cookie.CookieUnreadableError` if a cookie is
    present but unreadable; the caller turns either into a refusal to
    serve (AC3). BOTH refuse rather than replace: an unreadable cookie
    that gets re-minted strands every session holding the old value.

    CALLED BEFORE THE BIND, NEVER BESIDE THE DISCOVERY WRITE. A listener
    that binds first and checks second has already been reachable on a
    port it could not protect, which is the whole failure this guard
    exists to prevent.

    `ensure`, never `mint` -- see `cookie.ensure`. Minting here would
    rotate the secret at every engine boot and strand every session
    launched before it: the refuted lifetime, not the policy.

    Ordered ensure-then-assert because the directory is created by the
    first write, so the assertion needs something to read.
    """
    cookie.ensure(root)
    cookie.assert_directory_private(root)


def main() -> int:
    """The supervisor process entrypoint `ensure_listener`'s spawn trigger
    targets. Boot sequence, mirroring `warm.server.main`'s numbered steps:

    1. Elect this generation's DISTINCT (`http.`-prefixed) endpoint as a
       per-machine lock -- a named pipe on Windows, an `flock` on POSIX
       (`_elect_supervisor_slot`, which is also where the POSIX arm's absence
       until 2026-09-02 is recorded). `ElectionLost` means another process
       already supervises this clone's http route; exits 0, touches nothing.
    2. Bind a real TCP listener on `127.0.0.1:0` -- port 0 is an
       OS-assigned ephemeral port, the port-discovery scope item's actual
       mechanism: no fixed port to collide across 50-70 concurrent
       sessions on one machine.
    3. Declare this process's execution route (`server._declare_execution_route`,
       reused rather than a second copy) BEFORE the request-handling context is
       built -- every op-latency row a `/hook` fire writes stamps `warm_server`
       from this point on, instead of the `in_process` default every prior boot
       left in place (AC2: the route must be provable from telemetry, not timing).
    4. Write the discovery record (port, pid, birth epoch, generation sha)
       -- only reachable past step 1, so a process that lost the election
       never clobbers the winner's record.
    5. Serve forever until `lifecycle.begin_shutdown` (bound to `_stop`)
       ends the process.
    """
    root = _default_engine_clone()

    try:
        handle = _elect_supervisor_slot(root)
    except election.ElectionLost as exc:
        print(
            f"[warm-http-supervisor] election lost for {exc.endpoint!r}; another "
            "process already supervises this clone's http route, exiting",
            file=sys.stderr,
        )
        return 0

    # this process's life: `FILE_FLAG_FIRST_PIPE_INSTANCE` exclusion is a
    # property of a LIVE PIPE INSTANCE, not of the electing process, so

    from http.server import ThreadingHTTPServer

    class _NotYetBound:
        pass

    try:
        _assert_credential_ready(root)
    except (cookie.DirectoryNotPrivateError, cookie.CookieUnreadableError) as exc:
        print(
            f"[warm-http-supervisor] refusing to serve: {exc}",
            file=__import__("sys").stderr,
        )
        _release_election_handle(handle)
        return 3

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NotYetBound)
        version_state = skew.ServerVersionState(root)

        _declare_execution_route()

        ctx = _ServerContext(httpd=httpd, engine_root=root, version_state=version_state, election_handle=handle)
    except BaseException:
        _release_election_handle(handle)
        raise
    httpd.RequestHandlerClass = _make_handler(ctx)

    port = httpd.server_address[1]

    try:
        write_discovery(
            port=port,
            pid=os.getpid(),
            stable_pid_start_epoch=_self_stable_pid_start_epoch() or 0,
            engine_sha=skew.compute_client_token(root),
            engine_root=root,
        )
    except Exception as exc:  # noqa: BLE001 -- a HINT writer failing must not stop the server
        print(f"[warm-http-supervisor] failed to write discovery record: {exc!r}", file=__import__("sys").stderr)

    try:
        threading.Thread(target=ctx._skew_watchdog_loop, daemon=True, name="warm-http-skew-watchdog").start()
        httpd.serve_forever()
    finally:
        ctx.ctx_shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
