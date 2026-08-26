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
    - fail-open parity (P12) -- `ensure_listener()` NEVER waits for a
      listener to come up (mirrors `warm.client`'s "NO CLIENT EVER WAITS
      FOR A SERVER TO BOOT") and returns `None` on every failure mode
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
  - Does NOT wait for a listener to come up, ever -- see fail-open bullet
    above.
"""

from __future__ import annotations

import calendar
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root

from coordinator_core import locked_write
from coordinator_core.session.core import stable_pid_alive
from coordinator_core.warm import breadcrumb, election, hook_http, lifecycle, skew
from coordinator_core.warm.http_listener import _collect_response, _frame_from_request
from coordinator_core.warm.server import InFlightCounter, _declare_execution_route, _serve_line

__all__ = [
    "DISCOVERY_FILENAME",
    "HEALTH_PATH",
    "HOOK_PATH",
    "HEALTH_CHECK_TIMEOUT_SECS",
    "SPAWN_DEBOUNCE_SECS",
    "ENTRY_SCRIPT",
    "discovery_path",
    "write_discovery",
    "read_discovery",
    "unlink_discovery",
    "discovery_is_live",
    "should_spawn",
    "listener_url",
    "check_health",
    "supervisor_pipe_name",
    "ensure_listener",
    "main",
]

# Distinct filename in the SAME per-clone, per-user `svc_dir()` the pipe
# server's `warm.json` lives in -- see module docstring's negative-spec for
# why this is a second file, never a second shape inside the first.
DISCOVERY_FILENAME = "warm-http.json"

HEALTH_PATH = "/health"

# Re-exported from `hook_http`, which owns routing (`op_for_path`): the endpoint and the
# op a bare POST resolves to are one decision, and two spellings of it would let the
# transport 404 a path the router accepts. `write_discovery` publishes THIS name, so
# every existing reader keeps the name it already reads.
HOOK_PATH = hook_http.HOOK_PATH

# The op name `/hook` dispatches through `_serve_line`, registered by
# `coordinator_core/ops/warm_guard_evaluate.py` (landed 2026-08-25, state/handoffs/
# 2026-08-23-the-warm-guard-op-gets-registered.md). A `/hook` POST therefore resolves to
# a real guard verdict computed by the same `bash_guards.dispatch.evaluate_payload_json`
# chain every cold hook invocation runs.
#
# The METHOD_NOT_FOUND path this constant used to describe is not dead, only no longer
# the everyday case: a repoint of this name without a matching `@register_op` key, or an
# engine clone predating the op, still lands there, and `hook_http.interpret_result`
# still turns it into a loud "guard did not run" response -- never a fabricated allow,
# never a fabricated deny. Repointing this constant means repointing the registration in
# the same change.
GUARD_OP_NAME = hook_http.DEFAULT_OP_NAME

# A liveness probe, not a work budget -- mirrors `warm.client.
# READ_DEADLINE_SECS`'s "is the server wedged" framing, sized the same
# (2.0s) since both ask the identical question of a sibling transport.
HEALTH_CHECK_TIMEOUT_SECS = 2.0

# Reuses `breadcrumb.SPAWN_DEBOUNCE_SECS` rather than defining a second
# debounce window that could drift from the pipe transport's.
SPAWN_DEBOUNCE_SECS = breadcrumb.SPAWN_DEBOUNCE_SECS

# C9 (AC18/AC19): how often the skew watchdog re-checks its own boot token
# against a live `skew.compute_client_token` read. Same value and framing as
# `warm.server._IDLE_WATCHDOG_POLL_SECS` -- both ask "has this generation
# been superseded" on their own thread, independent of request traffic; this
# transport takes the identical number rather than inventing a second one.
# NOT an idle-demotion poll (module docstring negative-spec still holds: no
# idle watchdog here) -- this thread checks exactly one thing, staleness,
# and never reads served-count or seconds-idle.
_SKEW_WATCHDOG_POLL_SECS = 5.0

# `spawn_detached` respawns by the resolved interpreter path against this
# file itself (mirrors `warm.client.SERVER_ENTRY_SCRIPT` / `warm.server`'s
# own `if __name__ == "__main__":` entry).
ENTRY_SCRIPT = "coordinator_core/warm/supervisor.py"


def _default_engine_clone() -> Path:
    """This module's own resolved clone root -- collapsed onto the single
    shared definition, `engine_root.current_engine_clone()` (plan
    2026-08-19-an-engine-root-is-a-stamped-build § C3)."""
    return current_engine_clone()


def discovery_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/warm-http.json` for `engine_root` -- `breadcrumb.svc_dir`
    reused as a pure per-clone/per-user directory resolver, never mutated."""
    return breadcrumb.svc_dir(engine_root) / DISCOVERY_FILENAME


# The atomic-replace primitive now lives in `locked_write.replace_with_retry`,
# lifted there once this site had paid for it: the Windows sharing-violation
# window is a SHAPE of bug -- any atomic publish whose readers take no lock has
# it -- and `locked_rmw` had the identical unguarded `os.replace`, on the hook
# path, via `hooks/track_touched_files.py`. Reusing it rather than keeping a
# second copy here is the whole point; the two sites differ only in what they do
# when the budget expires, which is the boolean the helper returns.
# The reader's budget is far smaller than the writer's: it sits on the hook
# path, so a contended read must resolve in single-digit milliseconds or give up
# and let the caller fall open. The window it covers is one rename, not one
# write.
_READ_RETRY_BUDGET_SECS = 0.05
_READ_RETRY_SLEEP_SECS = 0.001

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
    """Write the discovery record under `locked_write.held_lock`, replacing
    any prior content -- mirrors `breadcrumb.write_breadcrumb`'s own
    "snapshot of the current boot, not an append log" contract exactly,
    for the same reason: the only reader that matters wants the LATEST
    listener, never a history. Never raises past a lock timeout or an
    `OSError` writing the file -- a caller asking this module to RECORD a
    boot needs to know if that recording failed.
    """
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
    # WRITERS; it does nothing for the reader, because `read_discovery` takes
    # no lock at all and must not -- it sits on the hook path, where a lock
    # acquisition is exactly the cost this transport exists to remove.
    #
    # `path.write_text` truncates first, so a lock-free reader landing inside
    # that window sees an empty or partial file, fails to parse, and gets
    # `None` -- which every consumer correctly reads as "no listener". The
    # listener is up the whole time.
    #
    # MEASURED, not theorised (doe-claude-5a's sink, 2026-08-25, n=445): two
    # isolated `no_listener` samples at 19:57:00.560Z and 19:58:00.562Z with
    # `probe_latency_ms` of **0.037 and 0.031 ms** against 116-167ms for
    # healthy neighbours -- thirty-odd MICROSECONDS, three orders of magnitude
    # short of one round trip, so nothing was dialled and no timeout was hit.
    # Both coincide with an `engine_token` rotation (`9331f66301a0bfe8` ->
    # `c529e3ea2af27a5f`), i.e. a publish rewriting this record, while OS
    # process-table evidence shows the listener pid ran continuously across
    # both. The record went away, never the process.
    #
    # mkstemp in the TARGET'S OWN DIRECTORY so `os.replace` is a same-volume
    # rename and therefore atomic on both Windows and POSIX; a temp file
    # elsewhere degrades to a copy and reopens the window it closes.
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
                # the very shape the atomic replace above exists to avoid -- so it is taken
                # ONLY when there is no record to damage.
                #
                # CORRECTED 2026-08-25, on evidence. The original wrote in place
                # unconditionally, reasoning that record availability outranks single-write
                # atomicity. That holds when the destination is ABSENT and INVERTS when it is
                # PRESENT, because the two failure modes are not equally loud:
                #   - a STALE record names a listener whose `engine_sha` no longer matches, so
                #     `_serve_line` answers ENGINE_SKEW: LOUD, and it evicts, so the next fire
                #     boots a current listener.
                #   - a TORN record reads as `None`, which every consumer correctly reads as
                #     "no listener": SILENT, and on the hook path that is a guard that did not
                #     run and said nothing.
                # Trading a loud stale read for a silent absent one is the wrong way round.
                # The observed events are `engine_token` rotations -- rewrites of a record
                # that ALREADY EXISTS -- which is exactly where the old fallback did harm.
                # Two post-`dcf4f83a1` events 36 minutes apart (2026-08-25T21:22:00.639Z and
                # T21:58:30.667Z, both k=1, surfaced by doe-claude-ec and -5a off the
                # committed sink) are a RATE, not stragglers.
                #
                # Skipping this rotation is safe: the record on disk stays readable and the
                # next rotation retries. `exists()` races a concurrent writer benignly --
                # losing that race skips one publish rather than tearing a record.
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
    """Read and parse the discovery record, or return None if absent,
    unreadable, or not a well-formed JSON object -- never raises, mirrors
    `breadcrumb.read_breadcrumb`'s HINT contract: every consumer must treat
    `None` as "no information," not an error.

    RETRIES ONLY ON FAILURE, so the happy path pays NOTHING -- this function is
    on the hook path and the whole point of the transport is what it does not
    spend. A first read that parses returns immediately, exactly as before.

    Why a retry exists at all: `write_discovery` now swaps the record in with
    `os.replace`, which closes the torn-read window but hands the reader a
    different, much narrower one -- on Windows an open racing a rename loses
    with a sharing violation (`PermissionError`), and a `None` from that is
    indistinguishable to every caller from "there is no listener". The record
    is rewritten on each engine-token rotation (five in one 213-minute
    observation window), and the incident this whole path was hardened for was
    exactly a reader seeing no record while the listener process ran
    continuously.

    A missing file is NOT retried: `FileNotFoundError` means no listener has
    ever published here, which is a real answer available immediately, and
    spinning on it would put the retry cost on the genuinely-cold path where it
    buys nothing. Only a contended read and a torn parse are retried.
    """
    path = discovery_path(engine_root)
    deadline: Optional[float] = None
    while True:
        retryable = False
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            retryable = True
            text = ""
        if not retryable:
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                retryable = True
                record = None
            if not retryable:
                return record if isinstance(record, dict) else None

        # Fail OPEN once the budget is spent -- a caller that cannot read the
        # record must be told "no information", never made to wait or raise.
        now = time.monotonic()
        if deadline is None:
            deadline = now + _READ_RETRY_BUDGET_SECS
        elif now >= deadline:
            return None
        time.sleep(_READ_RETRY_SLEEP_SECS)


def unlink_discovery(engine_root: Optional[Path] = None) -> None:
    """Best-effort remove the discovery file -- mirrors `breadcrumb.
    unlink_breadcrumb`'s never-raises contract."""
    path = discovery_path(engine_root)
    try:
        path.unlink()
    except OSError:
        pass


def discovery_is_live(record: dict) -> bool:
    """True iff `record`'s `pid` is still the SAME process that wrote it
    (`stable_pid_alive`, pid PLUS stored birth instant -- a recycled pid
    reads dead, exactly as `breadcrumb.should_spawn`'s own comparison
    does). Any malformed field, or a `stable_pid_alive` failure (e.g.
    `MissingPsutilError`), degrades to "cannot vouch for this record" ->
    False, never raises -- same HINT contract as every other read here.
    """
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
    structurally identical to `breadcrumb.should_spawn`, duplicated rather
    than parameterized into that function because `breadcrumb.py` sits
    outside this chunk's `writes:` (module docstring's negative-spec).
    True iff nothing currently vouches for an in-flight listener boot.
    """
    record = read_discovery(engine_root)
    if record is None:
        return True

    started_at = record.get("started_at")
    if not isinstance(started_at, str):
        return True
    try:
        started_epoch = calendar.timegm(time.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return True

    current = time.time() if now is None else now
    age = current - started_epoch
    if age < 0 or age >= SPAWN_DEBOUNCE_SECS:
        return True

    return not discovery_is_live(record)


def listener_url(record: dict) -> Optional[str]:
    """The base URL a live `record` describes, or None if `port` is
    missing/malformed."""
    port = record.get("port")
    if not isinstance(port, int):
        return None
    return f"http://127.0.0.1:{port}"


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
    """The per-machine election's pipe name -- `warm.election.pipe_name`
    reused verbatim, under an `"http."`-prefixed engine_token so this
    election can NEVER collide with `warm.server.main`'s own pipe-transport
    election on the identical clone (same SID, same clone hash, different
    token namespace -- `election.pipe_name`'s own docstring names the token
    as the one component this module is free to choose).
    """
    root = engine_root if engine_root is not None else _default_engine_clone()
    token = skew.compute_client_token(root)
    return election.pipe_name(f"http.{token}", engine_clone=root, user_sid=user_sid)


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

    NEVER WAITS -- mirrors `warm.client`'s "NO CLIENT EVER WAITS FOR A
    SERVER TO BOOT" doctrine verbatim, for the identical reason: with idle
    demotion (this package's `warm.idle`), "no listener yet" is the
    ordinary first call after any quiet period, not a rare cold start.

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
    """This process's own birth instant, in the SAME derivation `warm.
    server._self_stable_pid_start_epoch` uses -- kept as a local copy per
    this package's convention rather than importing that private name."""
    from coordinator_core.session.core import _win_create_time_epoch

    try:
        return _win_create_time_epoch(os.getpid())
    except Exception:
        return None


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
    ) -> None:
        self.httpd = httpd
        self.engine_root = engine_root
        self.in_flight = InFlightCounter()
        self.version_state = version_state
        self.server_sha = version_state.server_sha
        # `dispatch` overrides `_serve_line`'s own default (`_run_dispatch`) -- production
        # never sets it; a test does, standing in for the real registered op
        # `GUARD_OP_NAME` names (`warm_guard.evaluate`, `ops/warm_guard_evaluate.py`) so
        # it can drive a chosen verdict without running the full guard chain.
        self.dispatch = dispatch
        self.engine_token = self._compute_engine_token()
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

    def drain(self) -> None:
        """The `drain` `_serve_line` requires for a detected skew eviction. A no-op here:
        unlike the pipe transport's bounded worker pool, this listener has no queue of
        pending connections to drain -- `close_listener` (below) already stops accepting
        new ones, which is the whole of what this transport owes on skew."""
        return None

    def close_listener(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001 -- best-effort, mirrors ctx_shutdown's own contract
            pass

    def ctx_shutdown(self) -> None:
        self._skew_watchdog_stop.set()
        unlink_discovery(self.engine_root)

    def stop(self) -> None:
        lifecycle.begin_shutdown(
            close_listener=self.close_listener,
            in_flight_count=self.in_flight,
            ctx_shutdown=self.ctx_shutdown,
        )

    def _token_is_stale(self) -> bool:
        """C9 (AC18/AC19): has a publish rotated the engine stamp underneath
        this listener since it booted? Compares this context's own
        `engine_token` (self-stamped once at construction, see
        `_compute_engine_token`) against a LIVE `skew.compute_client_token`
        read -- mirrors `warm.server._ServerContext._token_is_stale`'s exact
        comparison, at this transport's own boot-token field.

        Never raises: this runs on the skew-watchdog thread every poll, and
        a transient stat failure (a stamp file mid-rewrite, an unresolvable
        `engine_root`) must not kill the watchdog. Any failure to establish
        the live token, or a `None` boot token (an unstamped root at
        construction -- `_compute_engine_token`'s own fail-open), reads as
        NOT stale: the safe default is "wait for the next poll," never a
        false eviction of a healthy listener.
        """
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
        no-op otherwise."""
        if self._token_is_stale():
            self.stop()

    def _skew_watchdog_loop(self) -> None:
        """Runs on its OWN thread, independent of the accept loop -- so a
        stale listener is retired even while it takes NO `/hook` traffic at
        all (the exact gap C9 closes; see this class's docstring).
        `Event.wait` both sleeps and gives `ctx_shutdown` a way to end this
        thread promptly once some OTHER trigger has already won the
        shutdown guard, mirroring `warm.server._ServerContext.
        _idle_watchdog_loop`'s identical use of its own stop event."""
        while not self._skew_watchdog_stop.wait(_SKEW_WATCHDOG_POLL_SECS):
            self._skew_watchdog_tick()


def _make_handler(ctx: "_ServerContext"):
    """Build a `BaseHTTPRequestHandler` subclass bound to `ctx` via
    closure -- `http.server`'s own idiom for per-server handler state,
    avoiding a module-level global `ctx`."""
    from http.server import BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Silence stdlib's default stderr access log -- this process
            # has no operator watching its console (module docstring's
            # "resident listener process").
            pass

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
            # `_serve_line` releases the in-flight slot itself, on the skew-eviction
            # path, before this handler ever gets a response back -- so this closure
            # (not a second `ctx.in_flight.exit()` call) is what both `_serve_line` and
            # this method's own `finally` share, exactly as the pipe transport's
            # `_exit_once` does (`warm.server._handle_connection`).
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

                # Reaching the guard op by ANY path -- bare `/hook` or the explicit
                # `/hook/warm_guard.evaluate` alias -- still requires the posted event
                # be one the guard chain can actually evaluate. Naming the op explicitly
                # does not supply the `tool_name`/`tool_input` the chain reads; a
                # SessionStart posted to either spelling would otherwise get a confident
                # verdict on a question nobody asked. Both spellings resolve to the same
                # `op_name`, so both get the same eligibility check.
                # Review: coordinator:code-reviewer -- explicit /hook/<op> alias bypassed
                # the bare-/hook safety check because it resolves to the same DEFAULT_OP_NAME
                # but failed the path-based exclusion; gate on op_name alone instead.
                if op_name == hook_http.DEFAULT_OP_NAME:
                    if hook_http.route_for_event(event_name) is None:
                        self._respond_json(hook_http.unserved_response(event_name))
                        return

                # posted JSON -> hook_http.payload_from_event (inside build_request)
                #             -> request frame (http_listener._frame_from_request)
                #             -> warm.server._serve_line
                #             -> http_listener._collect_response
                #             -> hook_http.interpret_result
                # THE CALLER'S ENVIRONMENT ARRIVES IN HEADERS, NOT IN THE BODY, and this
                # is where it is put back onto the event so `payload_from_event` finds it
                # where its contract says to look. The harness posts no `env` key under any
                # spelling -- measured, not assumed -- so without this the override boundary
                # is dead on this transport and reports "caller set no overrides" forever.
                header_env, disarm_reason = hook_http.env_from_headers(self.headers)
                if disarm_reason is not None:
                    # A DECLARED-BUT-VETOED CHANNEL IS AN UNRUN GUARD, NOT A CLEAN ONE. The
                    # veto empties overrides silently and the guard would read that as "no
                    # override requested" -- the permissive direction. Refusing to answer is
                    # this plan's anti-scope as code, the same call `unreachable_response`
                    # already makes for a dead engine.
                    self._respond_json(
                        hook_http.unreachable_response(
                            event_name or "PreToolUse", disarm_reason
                        )
                    )
                    return
                if header_env:
                    event = {**event, "env": header_env}

                request_frame = hook_http.build_request(event, op_name)
                request_frame = _frame_from_request(request_frame, ctx.engine_token)

                serve_kwargs = {
                    "version_state": ctx.version_state,
                    "server_sha": ctx.server_sha,
                    "close_listener": ctx.close_listener,
                    "drain": ctx.drain,
                    "release_in_flight": _release_once,
                }
                if ctx.dispatch is not None:
                    serve_kwargs["dispatch"] = ctx.dispatch

                raw_response = _collect_response(request_frame, _serve_line, serve_kwargs)
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


def main() -> int:
    """The supervisor process entrypoint `ensure_listener`'s spawn trigger
    targets. Boot sequence, mirroring `warm.server.main`'s numbered steps:

    1. Elect this generation's DISTINCT (`http.`-prefixed) pipe as a
       per-machine lock -- `ElectionLost` means another process already
       supervises this clone's http route; exits 0, touches nothing.
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
    sid = election.current_user_sid()
    name = supervisor_pipe_name(root, user_sid=sid)

    try:
        handle = election.elect(name, user_sid=sid)
    except election.ElectionLost:
        print(
            f"[warm-http-supervisor] election lost for {name!r}; another "
            "process already supervises this clone's http route, exiting",
            file=__import__("sys").stderr,
        )
        return 0

    # The election handle is a pure LOCK here (module docstring's
    # per-machine-election bullet) -- never used as a transport, unlike
    # `warm.server`'s own election handle. Closed immediately: the atomic
    # first-instance win already happened, and this process's lifetime,
    # not an open handle, is what a competitor's own `ElectionLost` check
    # observes.
    import _winapi

    try:
        _winapi.CloseHandle(handle)
    except Exception:  # noqa: BLE001 -- best-effort close of a won lock
        pass

    from http.server import ThreadingHTTPServer

    # `ctx` needs `httpd` to bind its `close_listener`, and the handler
    # class needs `ctx` -- so the server is constructed with a throwaway
    # handler class first, then `RequestHandlerClass` is swapped for the
    # real one before `serve_forever` ever dispatches a connection (the
    # attribute is only read PER REQUEST, in `BaseServer.finish_request`,
    # never at `__init__` time).
    class _NotYetBound:
        pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NotYetBound)
    version_state = skew.ServerVersionState(root)

    _declare_execution_route()

    ctx = _ServerContext(httpd=httpd, engine_root=root, version_state=version_state)
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

    # C9 (AC18/AC19): the skew-only watchdog, on its own thread, independent
    # of the accept loop -- see `_ServerContext`'s own docstring for why this
    # is not the idle watchdog `warm.server` runs. Started after the
    # discovery write so a poll landing before the first write sees this
    # context's own `engine_token`, never a torn boot sequence.
    threading.Thread(target=ctx._skew_watchdog_loop, daemon=True, name="warm-http-skew-watchdog").start()

    try:
        httpd.serve_forever()
    finally:
        ctx.ctx_shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
