"""coordinator_core.warm.front_door -- the fixed-port election, platform-correct
exclusivity, and foreign-holder detection for the shared HTTP front door.

Spec backlink: docs/plans/2026-08-25-the-bash-guard-stops-paying-for-a-process.md
§ C2 (AC3, AC4, AC4a). REVISED per eng-director review F3/F4/F5 -- the chunk that
authored this module was itself a revision of an earlier design (a machine-global
`election.py` namespace derived from the user SID); this docstring records the
outcome, not the superseded shape.

THE ELECTION IS THE BIND, NOT A SECOND LOCK. `election.py` elects a named pipe /
unix socket as a per-clone endpoint, and its own module docstring rejects
`CreateMutexW` for exactly the reason a pipe-based election over a
port-contested resource would reintroduce here: "a second identity that can
disagree with" the thing actually being contended for. The resource this module
contends for is a single, fixed, machine-global TCP port (`FIXED_PORT`) -- not a
pipe, not a socket file, not a SID-namespaced name. `socket.bind()` on that port
IS the election: kernel-atomic on both platforms, one identity, nothing left to
disagree with it. There is therefore no `pipe_name`-shaped namespace to derive
here at all, which is what closes the POSIX gap the prior (superseded) design
left open (F5) -- a TCP port has exactly one identity on both platforms, so
there is nothing platform-specific left to be incomplete about.

`FIXED_PORT`'S VALUE IS NOT INVENTED HERE. DoE's own forwarder
(`coordinator/hooks/http_hook_forwarder.py`, `DR-http-hook-forwarder-fixed-port.md`
Decisions 3/4) already binds `47623` as the one machine-global, OSS-safe fixed
port a static `type: "http"` registration can dial. The two sides of this
integration MUST agree on one literal or neither can ever reach the other, so
this module reuses that value rather than picking a second one that would need
reconciling later.

PLATFORM-CORRECT EXCLUSIVITY. Windows' `SO_REUSEADDR` permits a second socket to
silently coexist on an already-bound port -- measured in
`DR-http-hook-forwarder-fixed-port.md` Decision 4: two `SO_REUSEADDR` sockets
both bind the same fixed port with no error on the second, and which one serves
a connection is indeterminate. `SO_EXCLUSIVEADDRUSE` is the Windows flag that
turns that into a loud, immediate `EADDRINUSE` on the second bind attempt.
POSIX has no equivalent hardening flag and needs none: this module never sets
`SO_REUSEADDR` there, so the OS's ordinary first-bind-wins behaviour is the
whole guarantee -- asserted by test, the way `http_listener.bind_host()`
asserts its own bind contract, rather than left to a future edit to notice a
missing flag.

EADDRINUSE DISCRIMINATION (AC4, absorbing the former AC16). A second bind
attempt failing `EADDRINUSE` could mean two different things, and they carry
opposite verdicts: another instance of THIS SAME front door already won
(ordinary, expected, defer) -- or some OTHER process is squatting the fixed
port (a foreign holder, a stale artefact, a misconfigured neighbour). The
distinction is health-probed, never assumed: GET the existing holder's
`supervisor.HEALTH_PATH` and read the JSON body for this module's own
`door_protocol_version` marker (AC4a). A recognizable body -> `ElectionLost`
(ordinary defer, "lost to ourselves"). Anything else -- no answer, a timeout, a
non-2xx status, a malformed body, or a body missing the marker -- is a
`ForeignHolderError`: an explicit, loudly-typed state that must never be read
as "no listener" (a silent fall-through) nor as an ordinary lost election (a
silent defer to a process that is not us).

DOOR PROTOCOL VERSION (AC4a). A hand-bumped integer, deliberately distinct from
the skew/engine token (`skew.compute_client_token`), which rotates on EVERY
publish and would restart the door fleet-wide for engine changes that have
nothing to do with the door's own wire shape. `door_protocol_version()` below
is the sole source of the value this module's health payload publishes; C3's
discovery record reads the identical function rather than carrying a second
literal that could drift from it.

YIELD ON AN UNSTAMPED ROOT. `an-engine-root-is-a-stamped-build`'s doctrine
(DR-315) applies here exactly as it does to `supervisor.ensure_listener`'s own
`is_engine_root` gate: a front door whose own engine root is unstamped or
absent is not a real engine and must not compete for the port at all --
`elect_front_door` checks this FIRST, before any socket is touched, and raises
`UnstampedRootYield` rather than attempting (and winning, or losing) a bind on
behalf of a tree that is not a real build.

NEGATIVE SPEC:
  - No pipe/socket namespace of any kind is derived here -- see above. This
    module never calls `election.elect`, `election.pipe_name`, or
    `election.current_user_sid`; the whole point of the revision this module
    implements is that none of those apply to a port-contested resource.
  - No `SO_REUSEADDR` on either platform, and no `allow_reuse_address`
    convenience wrapper (e.g. `http.server.ThreadingHTTPServer`'s default) is
    used to construct the election socket, for the identical reason.
  - This module never spawns a process, never writes a discovery record, and
    never keeps the door alive past the moment of election -- that is C3's job
    (the process shell, discovery publication, and teardown around the
    primitive this module defines). This module owns exactly the moment of
    contention: bind, or discriminate why not.
"""

from __future__ import annotations

import calendar
import errno
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core import locked_write
from coordinator_core.session.core import stable_pid_alive
from coordinator_core.warm import breadcrumb, door_credential, front_door_routing, lifecycle, skew
from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root
from coordinator_core.warm.http_listener import bind_host
from coordinator_core.warm.server import InFlightCounter
from coordinator_core.warm.supervisor import HEALTH_PATH

__all__ = [
    "FIXED_PORT",
    "DOOR_PROTOCOL_VERSION",
    "DOOR_PROTOCOL_VERSION_KEY",
    "PROBE_TIMEOUT_SECS",
    "FrontDoorError",
    "ElectionLost",
    "ForeignHolderError",
    "UnstampedRootYield",
    "door_protocol_version",
    "door_health_payload",
    "is_own_door_health_payload",
    "probe_existing_holder",
    "elect_front_door",
    "DISCOVERY_FILENAME",
    "SPAWN_DEBOUNCE_SECS",
    "ENTRY_SCRIPT",
    "discovery_path",
    "write_discovery",
    "read_discovery",
    "unlink_discovery",
    "discovery_is_live",
    "should_spawn",
    "listener_url",
    "spawn_detached",
    "FLOOR_PROBE_RELATIVE_PATH",
    "FLOOR_PROBE_MARKER",
    "HTTP_HOOK_ENV_VAR_KEY",
    "HTTP_HOOK_ALLOWED_ENV_VARS_SETTINGS_KEY",
    "floor_violation",
    "http_hook_allowed_env_vars_violation",
    "ensure_front_door",
    "DIAL_COUNTER_FILENAME",
    "DIAL_COUNTER_SCHEMA",
    "DIAL_TAIL_MAX",
    "HOOK_EVENT_HEADER",
    "UNLABELLED_EVENT",
    "DIAL_FLUSH_MIN_INTERVAL_SECS",
    "dial_counter_path",
    "read_dial_counter",
    "DialCounter",
    "main",
]

#: The one machine-global, OSS-safe fixed port a static `type: "http"`
#: registration can dial -- see module docstring. Reused verbatim from DoE's
#: own forwarder (`coordinator/hooks/http_hook_forwarder.py :: FIXED_PORT`,
#: `DR-http-hook-forwarder-fixed-port.md`) rather than chosen independently:
#: both sides of this integration must agree on one literal.
FIXED_PORT = 47623

#: Hand-bumped door protocol version integer (AC4a) -- see module docstring's
#: "DOOR PROTOCOL VERSION" section for why this is distinct from the
#: skew/engine token. Bump this by hand when the door's own wire shape
#: (its health payload, its discovery-record fields) changes in a way an
#: older door's caller could not safely assume.
DOOR_PROTOCOL_VERSION = 1

#: The health-payload key `is_own_door_health_payload` looks for. A module
#: constant rather than an inline literal so a probe and a publisher can
#: never spell the marker differently.
DOOR_PROTOCOL_VERSION_KEY = "door_protocol_version"

#: A liveness probe budget, not a work budget -- mirrors
#: `supervisor.HEALTH_CHECK_TIMEOUT_SECS`'s own framing and value, since both
#: ask the identical question ("is the existing holder alive") of a sibling
#: transport.
PROBE_TIMEOUT_SECS = 2.0


class FrontDoorError(Exception):
    """Base for this module's own election failures."""


class ElectionLost(FrontDoorError):
    """`FIXED_PORT` is already bound by another instance of THIS front door.

    The health probe against the existing holder answered with a payload
    this module recognizes as its own (`is_own_door_health_payload`) -- the
    ordinary, expected outcome of a second process racing the same election.
    Defer to the existing holder; do not retry the bind.
    """

    def __init__(self, port: int):
        self.port = port
        super().__init__(
            f"lost front-door election for port {port}: an existing holder "
            "answered the health probe as our own door"
        )


class ForeignHolderError(FrontDoorError):
    """`FIXED_PORT` is bound by SOMETHING, but the health probe could not
    confirm it is this module's own front door.

    Covers every outcome other than a recognized door payload: no answer,
    a connection timeout, a non-2xx status, a malformed body, or a
    well-formed body missing the `door_protocol_version` marker. Must never
    be treated as `ElectionLost` (a real winner of the same election) nor
    silently read as "no listener up" -- both readings would hide a
    squatted port from whatever caller needs to know about it (AC4).
    """

    def __init__(self, port: int, detail: str):
        self.port = port
        self.detail = detail
        super().__init__(
            f"port {port} is held by a process that does not answer as our "
            f"own front door: {detail}"
        )


class UnstampedRootYield(FrontDoorError):
    """`engine_root` carries no valid engine stamp (DR-315 discipline).

    Raised BEFORE any socket is touched -- mirrors
    `supervisor.ensure_listener`'s own `is_engine_root` gate. A front door
    running against an unstamped tree is not a real engine build and must
    not compete for the fixed port on its behalf.
    """

    def __init__(self, engine_root: Path):
        self.engine_root = engine_root
        super().__init__(
            f"engine root {engine_root} is unstamped or absent; front door "
            "yields rather than electing"
        )


def door_protocol_version() -> int:
    """The hand-bumped door protocol version integer (AC4a).

    The sole source every publisher (this module's own health payload, C3's
    discovery record) reads, so the value can never diverge between the two
    surfaces.
    """
    return DOOR_PROTOCOL_VERSION


def door_health_payload() -> dict:
    """The JSON body this module's own `/health` endpoint publishes (AC4a).

    C3 wires the actual endpoint; this is the one place its shape is
    decided, so a probe (this module's own `EADDRINUSE` discrimination, or
    a peer front door doing the identical check) and the server that
    answers it can never disagree about the shape.
    """
    return {DOOR_PROTOCOL_VERSION_KEY: door_protocol_version()}


def is_own_door_health_payload(payload: Any) -> bool:
    """True iff `payload` (already-parsed JSON) carries a recognizable
    `door_protocol_version` marker.

    Deliberately permissive on the marker's INTEGER VALUE -- any int, not
    only the current `DOOR_PROTOCOL_VERSION` -- because a successor
    generation publishing a bumped version must still be recognized as an
    existing front door (AC4's "lost to ourselves" branch), never misread
    as a foreign process. Discriminating "is this version acceptable" is a
    separate question (AC4a's yield-on-lower-version path), not this one.
    """
    if not isinstance(payload, dict):
        return False
    return isinstance(payload.get(DOOR_PROTOCOL_VERSION_KEY), int)


def probe_existing_holder(
    port: int,
    *,
    timeout: float = PROBE_TIMEOUT_SECS,
    opener: Any = None,
) -> Optional[dict]:
    """GET `http://<bind_host()>:<port><HEALTH_PATH>` and return the parsed
    JSON body iff it looks like our own door's health payload
    (`is_own_door_health_payload`); `None` on ANY other outcome --
    connection refused, timeout, non-2xx status, malformed body, or a body
    missing the marker.

    Never raises: mirrors `supervisor.check_health`'s own liveness-probe
    contract. The caller (`elect_front_door`) treats `None` as "not
    recognizably ours" (AC4's foreign-holder branch), never as an error of
    this function's own.

    `opener` is an injectable `urllib.request.urlopen`-shaped callable for
    tests, mirroring `supervisor.check_health`'s own seam.
    """
    import urllib.request

    url = f"http://{bind_host()}:{port}{HEALTH_PATH}"
    open_url = opener if opener is not None else urllib.request.urlopen
    try:
        with open_url(url, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            if not (200 <= int(status) < 300):
                return None
            raw = resp.read()
    except Exception:  # noqa: BLE001 -- a probe must never raise, see docstring
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not is_own_door_health_payload(payload):
        return None
    return payload


def _apply_exclusivity(sock: socket.socket) -> None:
    """Set the platform-correct exclusivity flag -- `SO_EXCLUSIVEADDRUSE` on
    Windows, nothing on POSIX (module docstring's negative spec: no
    `SO_REUSEADDR` on either platform, so POSIX gets the OS's ordinary
    first-bind-wins guarantee with no flag at all)."""
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)


def _is_addr_in_use(exc: OSError) -> bool:
    """True iff `exc` is the platform's address-in-use error.

    POSIX raises `errno.EADDRINUSE`. Windows' socket module maps the
    underlying `WSAEADDRINUSE` (10048) onto `errno.EADDRINUSE` in the
    common case, but the raw `winerror` is checked too so a bind failure is
    never mistaken for a genuine in-use collision under either attribute.
    """
    if getattr(exc, "errno", None) == errno.EADDRINUSE:
        return True
    return getattr(exc, "winerror", None) == 10048


def elect_front_door(
    *,
    engine_root: Optional[Path] = None,
    port: int = FIXED_PORT,
    probe_opener: Any = None,
    probe_timeout: float = PROBE_TIMEOUT_SECS,
) -> socket.socket:
    """Bind `port` on the loopback address -- THE election (module
    docstring's "THE ELECTION IS THE BIND" section).

    Returns the bound, listening socket on a win. Raises on every loss, and
    the three losses are distinctly typed so a caller (C3's process shell)
    can `except` them separately rather than string-matching a message:

      - `UnstampedRootYield` -- `engine_root` (default: this repo's resolved
        clone, `engine_root.current_engine_clone()`) carries no valid
        engine stamp. Checked FIRST, before any socket is touched.
      - `ElectionLost` -- `EADDRINUSE`, and a health probe against the
        existing holder returned a payload this module recognizes as its
        own door (AC4).
      - `ForeignHolderError` -- `EADDRINUSE`, and the health probe did NOT
        recognize the existing holder (AC4's explicit, loud foreign-holder
        state).

    Any other `OSError` at bind time (not an address-in-use collision) is
    re-raised unchanged -- this function discriminates the one ambiguous
    case (`EADDRINUSE`), it does not swallow unrelated bind failures.
    """
    root = engine_root if engine_root is not None else current_engine_clone()
    if not is_engine_root(root):
        raise UnstampedRootYield(root)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _apply_exclusivity(sock)
    try:
        sock.bind((bind_host(), port))
    except OSError as exc:
        sock.close()
        if not _is_addr_in_use(exc):
            raise
        payload = probe_existing_holder(port, timeout=probe_timeout, opener=probe_opener)
        if payload is not None:
            raise ElectionLost(port) from exc
        raise ForeignHolderError(port, detail=str(exc)) from exc

    sock.listen(socket.SOMAXCONN)
    return sock


# ---------------------------------------------------------------------------
# C3 -- the process shell around the C2 election primitive.
#
# Spec backlink: docs/plans/2026-08-25-the-bash-guard-stops-paying-for-a-process.md
# § C3 (AC4a, AC10, AC13). Mirrors `supervisor.py`'s resident-listener shape
# rather than inventing a second one: spawn via `ops.ceremony.detached_spawn.
# spawn_detached` (the SAME lazy-imported wrapper, mirroring `supervisor.
# spawn_detached`'s own docstring on why the import stays inside the function),
# shutdown via `warm.lifecycle`'s single ordered sequence, in-flight
# accounting via `warm.server.InFlightCounter`.
#
# DISCOVERY IS DUPLICATED, NOT IMPORTED, from `supervisor.py`, for the same
# reason `supervisor.should_spawn`'s own docstring gives for duplicating
# `breadcrumb.should_spawn`: the two modules publish distinct records (a
# per-clone ephemeral-port listener vs. this module's fixed-port, one-per-
# machine door) under distinct filenames in the same per-clone `svc_dir()`,
# and a caller reading `supervisor`'s record must never be handed this
# module's shape by accident.
#
# NEGATIVE SPEC (this section only):
#   - No routing / op dispatch lives here. `main()`'s handler answers only
#     `GET <HEALTH_PATH>` with `door_health_payload()` (AC4a's own publisher);
#     `/hook`-shaped POST routing is C4's job (`front_door_routing.py`,
#     a separate surface), not this chunk's.
#   - `ensure_front_door()` here is the NARROW primitive C3's chunk body
#     names: read the discovery record, confirm it is live AND at least this
#     module's own `door_protocol_version()` (generation-aware), else
#     best-effort spawn and return `None` this call. The floor-ASSERTION,
#     idempotent-spawn-under-concurrency, and `httpHookAllowedEnvVars`
#     checks (AC7/AC8) are C6's job, layered onto this same function's name
#     in a later chunk -- not reimplemented here.
#   - Never waits, never raises (AC10) -- every function below that can fail
#     degrades to `None`/`False`, mirroring `supervisor.py`'s identical
#     contract line for line.
# ---------------------------------------------------------------------------

#: Distinct filename in the SAME per-clone, per-user `svc_dir()` `supervisor.
#: DISCOVERY_FILENAME` (`warm-http.json`) lives in -- see section docstring
#: above for why this is a second file, never a second shape inside the
#: first.
DISCOVERY_FILENAME = "warm-front-door.json"

#: Reuses `breadcrumb.SPAWN_DEBOUNCE_SECS` rather than defining a second
#: debounce window that could drift from the pipe transport's or
#: `supervisor`'s own -- identical reasoning to `supervisor.
#: SPAWN_DEBOUNCE_SECS`.
SPAWN_DEBOUNCE_SECS = breadcrumb.SPAWN_DEBOUNCE_SECS

#: `spawn_detached` respawns by the resolved interpreter path against this
#: file itself (mirrors `supervisor.ENTRY_SCRIPT` / `supervisor.main`'s own
#: `if __name__ == "__main__":` entry).
ENTRY_SCRIPT = "coordinator_core/warm/front_door.py"

# Same atomic-replace primitive `supervisor.write_discovery` uses -- see that
# module's own comment for why `os.replace` (not truncate-then-write) is the
# only safe publish shape for a lock-free reader on the hook path, and why
# the reader's retry budget is far smaller than the writer's.
_READ_RETRY_BUDGET_SECS = 0.05
_READ_RETRY_SLEEP_SECS = 0.001

_replace_with_retry = locked_write.replace_with_retry


def discovery_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/warm-front-door.json` for `engine_root` -- `breadcrumb.
    svc_dir` reused as a pure per-clone/per-user directory resolver, never
    mutated, exactly as `supervisor.discovery_path` reuses it."""
    return breadcrumb.svc_dir(engine_root) / DISCOVERY_FILENAME


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
    any prior content -- mirrors `supervisor.write_discovery`'s own
    "snapshot of the current boot, not an append log" contract, including
    the atomic-replace-with-in-place-fallback shape. Carries the door
    protocol version (AC4a) so `ensure_front_door`'s generation-aware read
    never needs a second call to learn it. Never raises past a lock timeout
    or an `OSError` writing the file.
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
        DOOR_PROTOCOL_VERSION_KEY: door_protocol_version(),
    }
    path = discovery_path(engine_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with locked_write.held_lock(path, holder_label="warm.front_door"):
        payload = json.dumps(record, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".front-door-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if not _replace_with_retry(tmp_path, str(path)):
                # Contended past the budget. Publish IN PLACE rather than
                # leave this clone with no record at all -- see
                # `supervisor.write_discovery`'s identical comment.
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
    `supervisor.read_discovery`'s HINT contract and identical retry-only-
    on-failure shape (a first read that parses pays nothing extra)."""
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

        now = time.monotonic()
        if deadline is None:
            deadline = now + _READ_RETRY_BUDGET_SECS
        elif now >= deadline:
            return None
        time.sleep(_READ_RETRY_SLEEP_SECS)


def unlink_discovery(engine_root: Optional[Path] = None) -> None:
    """Best-effort remove the discovery file -- teardown's other half
    (AC13, alongside releasing the binder claim by closing the listening
    socket). Mirrors `supervisor.unlink_discovery`'s never-raises contract."""
    path = discovery_path(engine_root)
    try:
        path.unlink()
    except OSError:
        pass


def discovery_is_live(record: dict) -> bool:
    """True iff `record`'s `pid` is still the SAME process that wrote it
    (`stable_pid_alive`, pid PLUS stored birth instant -- a recycled pid
    reads dead) -- AC13's own wording, identical mechanism to
    `supervisor.discovery_is_live` and `breadcrumb.should_spawn`. Any
    malformed field, or a `stable_pid_alive` failure, degrades to "cannot
    vouch for this record" -> False, never raises."""
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
    structurally identical to `supervisor.should_spawn` and `breadcrumb.
    should_spawn`. True iff nothing currently vouches for an in-flight
    front-door boot."""
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
    missing/malformed -- mirrors `supervisor.listener_url`."""
    port = record.get("port")
    if not isinstance(port, int):
        return None
    return f"http://{bind_host()}:{port}"


def spawn_detached(repo_root: str, script_path: str, args: Optional[Any] = None) -> bool:
    """Lazy delegate to `ops.ceremony.detached_spawn.spawn_detached` -- THE
    IMPORT IS INSIDE THE FUNCTION on purpose, mirroring `supervisor.
    spawn_detached`'s own docstring: importing `coordinator_core.ops`
    registers the entire op surface, and this module's own read/generation-
    check path (the overwhelming majority of `ensure_front_door` calls) must
    never pay that cost. Module-level name, not inlined at the call site, so
    a test can monkeypatch it the same way `supervisor`'s tests patch
    `supervisor.spawn_detached`."""
    from coordinator_core.ops.ceremony.detached_spawn import (
        spawn_detached as _spawn_detached_impl,
    )

    return _spawn_detached_impl(repo_root, script_path, args)


def _self_stable_pid_start_epoch() -> Optional[int]:
    """This process's own birth instant -- kept as a local copy per this
    package's convention (`supervisor._self_stable_pid_start_epoch`) rather
    than importing that private name."""
    from coordinator_core.session.core import _win_create_time_epoch

    try:
        return _win_create_time_epoch(os.getpid())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# C6 -- ensure_front_door() layers on: the floor assertion (AC8) and the
# httpHookAllowedEnvVars boot check (AC7). Both are BOOT-scoped, once-per-
# process advisories, never a functional gate on `ensure_front_door`'s own
# `Optional[str]`/never-raises contract (AC10) -- mirrors `warm.settings.
# _log_warm_disabled_once`'s exact shape (a module-scope bool, set once,
# never reset outside a test) for the identical reason that function states:
# a boot-time condition, not a per-call fact, so a per-call diagnostic would
# be pure noise on a resident server (warm.hook_http's own negative spec
# already forbids a per-fire stderr banner).
#
# NEGATIVE SPEC (this section only):
#   - Neither check changes `ensure_front_door`'s return value. A caller
#     that needs a machine-readable violation reads the (also once-per-
#     process) stderr line this section prints; the function itself keeps
#     answering only "a live, recognized front door's URL, or None".
#   - The floor discriminant is NEVER a claude-klabauter commit id (module docstring
#     precedent, AC8's revision): `probe_existing_holder`'s target already
#     runs the published mirror, which does not carry this repo's SHAs. The
#     probe here is the fix's OWN presence -- a marker symbol this exact
#     module (C2/C3's work) introduced -- read straight off the TARGET
#     `engine_root`'s own copy of this file, never off the currently-
#     importing module's `__file__` (which proves nothing about what
#     `spawn_detached`/C3's `ENTRY_SCRIPT` would actually run against that
#     root).
# ---------------------------------------------------------------------------

#: Where the floor probe reads FROM, relative to a candidate `engine_root`
#: -- this module's own path inside the tree being asked to serve as the
#: front door, so the probe answers "does THAT clone carry the fix", not
#: "does the process asking carry it" (the two can differ under a
#: correct-looking forwarder pointing at a stale clone, AC8's own framing).
FLOOR_PROBE_RELATIVE_PATH = Path("coordinator_core") / "warm" / "front_door.py"

#: A symbol this exact chunk's own module introduced (C2/C3), searched for
#: verbatim in the TARGET clone's copy of `FLOOR_PROBE_RELATIVE_PATH`.
#: Presence proves "the fix is here in substance" without resolving any
#: git commit id -- the published mirror this seam runs against does not
#: carry this repo's SHAs at all (module docstring, AC8's revision).
FLOOR_PROBE_MARKER = "elect_front_door"

#: The exported, whitelisted, non-`CLAUDE_`-prefixed env var DoE's
#: `claude-doe.py` launcher exports as the clone-identity key the http hook
#: transport routes on (`DR-http-hook-forwarder-fixed-port.md` C1). Kept as
#: a bare literal here, mirroring `claude-doe.py`'s own bare
#: `os.environ.setdefault("COORDINATOR_CLONE_ROOT", ...)` -- there is no
#: shared constant module either side already imports, and inventing one
#: here for a single string would be a second source of truth, not a fix.
HTTP_HOOK_ENV_VAR_KEY = "COORDINATOR_CLONE_ROOT"

#: The harness `settings.json` top-level key AC7 asserts symmetrically at
#: boot. Absent entirely -> gates nothing (the registration's own
#: `allowedEnvVars` suffices, measured). Present but omitting
#: `HTTP_HOOK_ENV_VAR_KEY` -> a settings-level veto that blanks clone
#: identity and denies every http hook fire on this machine -- the
#: fleet-wide deny-everything AC7 exists to catch loudly.
HTTP_HOOK_ALLOWED_ENV_VARS_SETTINGS_KEY = "httpHookAllowedEnvVars"

_floor_violation_announced = False
_http_hook_allowed_env_vars_violation_announced = False


def _reset_boot_assertions_for_test() -> None:
    """Test-only seam: clear both once-per-process boot-assertion flags."""
    global _floor_violation_announced, _http_hook_allowed_env_vars_violation_announced
    _floor_violation_announced = False
    _http_hook_allowed_env_vars_violation_announced = False


def floor_violation(engine_root: Path) -> Optional[str]:
    """A one-line violation message iff `engine_root`'s own copy of this
    module does not carry `FLOOR_PROBE_MARKER` -- an older clone under a
    correct-looking forwarder, AC8's exact concern. `None` when the marker
    is present, when `engine_root` has no such file at all (also a
    violation -- an absent file cannot carry the fix), is read as `None`
    ONLY when the marker is found; every other outcome (missing file,
    unreadable file) is a real, reportable violation, never a silent pass.

    Never raises: an `OSError` reading the probe path is itself the
    violation ("cannot confirm the fix is here"), not a reason to fail
    open on the read and report nothing (module docstring: "a check that
    cannot resolve its reference reports a violation that is not real, or
    fails open and reports nothing; both are worse than no check").
    """
    probe_path = Path(engine_root) / FLOOR_PROBE_RELATIVE_PATH
    try:
        text = probe_path.read_text(encoding="utf-8")
    except OSError:
        return (
            f"engine root {engine_root} floor violation: could not read "
            f"{FLOOR_PROBE_RELATIVE_PATH} to confirm the front-door fix is "
            "present -- treated as an older clone under a correct-looking "
            "forwarder, not a silent pass"
        )
    if FLOOR_PROBE_MARKER in text:
        return None
    return (
        f"engine root {engine_root} floor violation: "
        f"{FLOOR_PROBE_RELATIVE_PATH} does not carry {FLOOR_PROBE_MARKER!r} "
        "-- an older clone under a correct-looking forwarder is serving as "
        "the front door"
    )


def _assert_floor_once(engine_root: Path) -> None:
    global _floor_violation_announced
    if _floor_violation_announced:
        return
    try:
        message = floor_violation(engine_root)
    except Exception:  # noqa: BLE001 -- an advisory must never raise into ensure_front_door
        return
    if message is None:
        return
    _floor_violation_announced = True
    print(f"[front-door] {message}", file=sys.stderr)


def http_hook_allowed_env_vars_violation(settings_path: Optional[Path] = None) -> Optional[str]:
    """A one-line violation message iff the harness `settings.json`
    (`coordinator_core.install.gen_settings_hooks.resolve_settings_out_path`
    -- reused, not re-derived, so this check and the installer's own
    generator can never disagree about which file is "the" settings.json)
    carries `HTTP_HOOK_ALLOWED_ENV_VARS_SETTINGS_KEY` but omits
    `HTTP_HOOK_ENV_VAR_KEY` from it (AC7's "present-but-omitting-our-key"
    shape).

    `None` for every other outcome: the file is absent, unreadable, not
    valid JSON, not a JSON object, or carries the key with our var
    included -- AC7's "absent gates nothing" shape, deliberately distinct
    from (never conflated with) the present-but-omitting-us shape this
    function exists to catch.
    """
    from coordinator_core.install.gen_settings_hooks import resolve_settings_out_path

    path = Path(settings_path) if settings_path is not None else Path(resolve_settings_out_path())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if HTTP_HOOK_ALLOWED_ENV_VARS_SETTINGS_KEY not in data:
        return None
    allowed = data[HTTP_HOOK_ALLOWED_ENV_VARS_SETTINGS_KEY]
    if isinstance(allowed, list) and HTTP_HOOK_ENV_VAR_KEY in allowed:
        return None
    return (
        f"settings.json ({path}) carries {HTTP_HOOK_ALLOWED_ENV_VARS_SETTINGS_KEY!r} "
        f"but omits {HTTP_HOOK_ENV_VAR_KEY!r} -- every http hook fire on "
        "this machine is denied fleet-wide by this untouched settings file"
    )


def _assert_http_hook_allowed_env_vars_once(settings_path: Optional[Path] = None) -> None:
    global _http_hook_allowed_env_vars_violation_announced
    if _http_hook_allowed_env_vars_violation_announced:
        return
    try:
        message = http_hook_allowed_env_vars_violation(settings_path)
    except Exception:  # noqa: BLE001 -- an advisory must never raise into ensure_front_door
        return
    if message is None:
        return
    _http_hook_allowed_env_vars_violation_announced = True
    print(f"[front-door] {message}", file=sys.stderr)


def ensure_front_door(
    engine_root: Optional[Path] = None,
    *,
    now: Optional[float] = None,
    probe_opener: Any = None,
    probe_timeout: float = PROBE_TIMEOUT_SECS,
    settings_path: Optional[Path] = None,
) -> Optional[str]:
    """The narrow, generation-aware autostart + fail-open entry point this
    chunk's body names. Returns a live, recognized front door's base URL, or
    `None` if none is reachable THIS call.

    NEVER WAITS FOR A BOOT, NEVER RAISES (AC10) -- identical contract to
    `supervisor.ensure_listener`, for the identical reason: a caller on the
    hook path must see "no reachable front door this call" and fall back to
    its own existing local path, never hang or raise.

    IT DOES WAIT UP TO `probe_timeout`, corrected 2026-08-26. The line above
    read a bare "NEVER WAITS" until the succession investigation checked it
    against the body: branch 1 calls `probe_existing_holder`, bounded by
    `PROBE_TIMEOUT_SECS` (2.0s), so a live-pid-but-hung holder costs this
    call the full timeout. Unlike `ensure_listener`'s identical mismatch,
    this one is NOT on the pipe server's boot path -- every call site was
    grepped (`docs/research/2026-08-26-repo-warm-succession.md`, specialist
    D § 3) and none is reached from `warm/server.py :: _run_guarded`.
    Preserve that distinction: the two functions share a defect in their
    documented contract and do not share its cost.

    1. A live discovery record (`discovery_is_live`, AC13) whose own
       `door_protocol_version` is at least this module's
       (`door_protocol_version()`, AC4a's "equal-or-higher, defer" branch)
       AND that answers the health probe as our own door
       (`probe_existing_holder`) -> its URL.
    2. Otherwise, if nothing currently vouches for an in-flight boot
       (`should_spawn`), best-effort spawn one and return `None` THIS call.
    3. Any other outcome also returns `None` -- fail open, no wait, no
       exception.

    A record naming a LOWER `door_protocol_version` than this call's own is
    deliberately NOT treated as live here -- it falls through to branch 2
    exactly as a dead record would, rather than being handed an orderly-
    yield request.

    C6's TWO BOOT-SCOPED ADVISORIES run first, unconditionally, once per
    process -- neither changes this function's return value (this section's
    own negative spec): the floor assertion (AC8, `_assert_floor_once`) and
    the `httpHookAllowedEnvVars` boot check (AC7,
    `_assert_http_hook_allowed_env_vars_once`). Both fire before the
    engine-root gate below, since the env-var check has nothing to do with
    any particular `engine_root` at all, and the floor check's own point is
    to catch a downgrade the gate below cannot see (a STAMPED but OLD
    clone still passes `is_engine_root`).

    GATED ON A STAMPED ENGINE ROOT -- identical reasoning to `supervisor.
    ensure_listener`'s own gate: an unstamped tree is not an engine, and
    spawning against it is pure litter on the operator's real machine
    (`svc_dir()` keys off the real `%LOCALAPPDATA%`, not a test's
    quarantine).
    """
    root = engine_root if engine_root is not None else current_engine_clone()
    _assert_floor_once(root)
    _assert_http_hook_allowed_env_vars_once(settings_path)
    if not is_engine_root(root):
        return None
    try:
        record = read_discovery(root)
        if record is not None and discovery_is_live(record):
            version = record.get(DOOR_PROTOCOL_VERSION_KEY)
            if isinstance(version, int) and version >= door_protocol_version():
                url = listener_url(record)
                if url is not None:
                    port = record.get("port")
                    if isinstance(port, int):
                        payload = probe_existing_holder(port, timeout=probe_timeout, opener=probe_opener)
                        if payload is not None:
                            return url

        if should_spawn(root, now=now):
            spawn_detached(str(root), ENTRY_SCRIPT)
        return None
    except Exception:  # noqa: BLE001 -- fail-open parity (AC10): never fail the caller
        return None


# ---------------------------------------------------------------------------
# The dial counter -- "did the harness dial?", answerable when the answer is ZERO
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. Whether the harness dials a `PreToolUse` `type: "http"`
# registration at all is **UNKNOWN**, and nothing on either side of this
# transport can currently answer it. That is the gap this counter closes.
#
# The history matters, because the first attempt to answer it got the answer
# wrong in the exact way this counter exists to prevent. DoE-claude `45e3673f`
# reported the registration SILENTLY INERT: a fresh session ran a command a
# guard denies, the write landed, so the harness had evidently never dialled.
# **That claim was RETRACTED by its own author within the hour**, and the
# retraction supersedes it: the guard in question is folded into DoE's
# `preuse-bash-dispatch.py` and runs LOCALLY, before the engine leg, so this
# engine was never going to object to that command and the write lands
# *whether or not the harness dialled*. The apparent control arm proved only
# that the `command` transport runs a larger guard set, which it does by
# construction.
#
# So the original probe INFERRED DISPATCH FROM A VERDICT -- precisely the
# error named by coordinator tripwire `AN-UNDIALED-HOOK-IS-NOT-A-PASSING-GUARD`,
# committed by the same author who wrote that tripwire, within an hour of
# writing it. It is now that tripwire's worked example of the error rather
# than evidence for it.
#
# THE DISCRIMINATION THIS COUNTER MUST MAKE, stated as the two hypotheses that
# probe could not separate: "dialled, and the guard returned no objection"
# versus "never dialled". A verdict cannot tell them apart, because both end
# in the operation proceeding. Only a receiver-side count can: `received > 0`
# is a dial, `received == 0` against a live `boot_at` is not.
#
# NEGATIVE SPEC -- WHAT THIS COUNTER CANNOT ANSWER, AND MUST NOT BE READ AS
# ANSWERING. It says a request ARRIVED. It says nothing about whether the
# guard set that ran on it is the same guard set the other transport runs.
# Those are two instruments, and conflating them is a live failure mode rather
# than a theoretical one: DoE's `type: "http"` registration posts to this
# engine and bypasses their `preuse-bash-dispatch.py`, so a flip silently
# drops the guards folded into that script. TWO TRANSPORTS ARE TWO GUARD
# POPULATIONS.
#
# Against a dropped guard, NEITHER safeguard fires. Fail-closed does not,
# because nothing fails -- the dropped guard was never invoked to fail. This
# counter does not either: the dial succeeds, the surviving guards answer
# normally, `received` moves, and the verdict is honest. The missing guard is
# missing from a population nobody counted. So a confirmed dial does NOT make
# a transport swap safe, and a green count here is not evidence that it is.
# Only an explicit before/after enumeration of the guard set answers that,
# and no such enumeration exists on either side of this seam yet.
#
# Coordinator tripwire `AN-UNDIALED-HOOK-IS-NOT-A-PASSING-GUARD`: no guard
# whose verdict can BLOCK an operation rides a transport whose dial is
# unverified, and that stays true when the transport demonstrably works,
# because WORKING and DIALLED are different facts. Fail-closed protects
# against a broken door; it never protects against an undialed one, because a
# fire that never arrives never reaches the deny either.
#
# NEGATIVE SPEC -- the read surface is a FILE, never an endpoint. A read that
# is itself a POST cannot answer "did anything POST": it conflates the
# instrument with the thing measured and perturbs the count by exactly the
# amount that makes zero unreadable. There is no `GET /dials` here, and one
# must not be added.
#
# NEGATIVE SPEC -- ABSENT IS NOT ZERO. The file is written AT BOOT with every
# count already at 0, so three readings exist where there was one silence:
#   file absent          -> the door never started; says nothing about dialling
#   received 0           -> door up since `boot_at`, harness NEVER DIALLED
#   received n           -> dialled n times
# Without the boot-time write, "zero" and "never booted" are the same bytes
# and the instrument has rebuilt the bug it exists to detect.
#
# Counted at the REQUEST LINE, before routing or validation, and split into
# `received` vs `dispatched`: "dialled but sent a shape we rejected" is a
# different diagnosis from "never dialled", and only a pre-validation count
# separates them.

#: Per hook event, never global: `PreToolUse` inert while `SessionStart`
#: carries traffic reads as "traffic exists" on a single global integer.
DIAL_COUNTER_FILENAME = "warm-front-door-dials.json"

DIAL_COUNTER_SCHEMA = 1

#: A nonzero count with no tail is uninterpretable to a caller varying one
#: registration field at a time -- the tail says WHICH variation dialled.
DIAL_TAIL_MAX = 16

#: The ONLY source of a per-event key. Never inferred from the path, never
#: defaulted to a plausible event name -- see `UNLABELLED_EVENT`.
HOOK_EVENT_HEADER = "X-Coordinator-Hook-Event"

#: Where an arrival with no `HOOK_EVENT_HEADER` is counted. Reserved, and
#: angle-bracketed so it can never collide with a real hook event name.
#:
#: This bucket is the point rather than a tidy-up: an unlabelled arrival that
#: is filed under a REAL event name is worse than one that is dropped, because
#: it inflates the very population the counter exists to interrogate. If the
#: question is "did the harness dial `PreToolUse`", then a nonzero
#: `PreToolUse` must mean the harness said `PreToolUse` -- not that something
#: arrived and we guessed. The arrival is still counted, still tailed with its
#: path, and still visibly a dial; it is simply not counted as a dial of an
#: event nobody claimed it was.
UNLABELLED_EVENT = "<unlabelled>"

#: The 0->1 transition is the one that matters, so it is flushed immediately.
#: Subsequent increments coalesce to at most one write per interval, because
#: a file write per Bash fire on a box running 50-70 sessions is exactly the
#: per-call cost this transport exists to remove (CLAUDE.md load norm).
DIAL_FLUSH_MIN_INTERVAL_SECS = 1.0


def _dial_now_iso() -> str:
    """UTC, second resolution, `Z`-suffixed -- the same shape the discovery
    record and the availability sink already use, so a reader correlating
    the two never has to reconcile two timestamp conventions."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def dial_counter_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/warm-front-door-dials.json` -- the same per-clone/per-user
    directory resolver `discovery_path` uses, so a reader who can find the
    discovery record finds this beside it with no second convention."""
    return breadcrumb.svc_dir(engine_root) / DIAL_COUNTER_FILENAME


def read_dial_counter(engine_root: Optional[Path] = None) -> Optional[dict]:
    """The counter as last flushed, or `None` when the file is absent.

    `None` means THE DOOR NEVER STARTED and says nothing about dialling -- it
    is not zero, and a caller that collapses the two has rebuilt the bug this
    exists to detect. A returned record's counts may lag a live door by up to
    `DIAL_FLUSH_MIN_INTERVAL_SECS`; the FIRST dial of any event is flushed
    immediately, so a zero that has never moved is never stale.
    """
    path = dial_counter_path(engine_root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 -- an unreadable read is not a verdict
        return None


class DialCounter:
    """Boot-scoped inbound-request tally, flushed atomically to disk.

    Never raises into a caller: this runs on the request path, and an
    instrument that can break the thing it measures is worse than no
    instrument. Every flush failure is swallowed and retried on the next
    increment.

    `boot_id` exists so a restart is not read as a decrement -- counts are
    monotonic WITHIN a lifetime, and a comparing caller compares
    `(boot_id, count)` pairs rather than bare integers.
    """

    def __init__(self, *, engine_root: Optional[Path], pid: Optional[int] = None) -> None:
        import uuid

        self.engine_root = engine_root
        self.boot_id = uuid.uuid4().hex
        self.boot_at = _dial_now_iso()
        self.pid = os.getpid() if pid is None else pid
        self.events: dict = {}
        self.tail: list = []
        self.last_received_at: Optional[str] = None
        self._lock = threading.Lock()
        self._last_flush_at = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "schema": DIAL_COUNTER_SCHEMA,
                "boot_id": self.boot_id,
                "boot_at": self.boot_at,
                "pid": self.pid,
                "door_protocol_version": door_protocol_version(),
                "last_received_at": self.last_received_at,
                "events": {k: dict(v) for k, v in self.events.items()},
                "tail": list(self.tail),
            }

    def flush(self) -> bool:
        """Atomic tmp+rename publish, mirroring `write_discovery`'s shape so a
        lock-free reader never sees a torn file. Returns success; never
        raises."""
        path = dial_counter_path(self.engine_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
            tmp_path = str(path) + f".{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
            if not _replace_with_retry(tmp_path, str(path)):
                return False
            self._last_flush_at = time.time()
            return True
        except Exception:  # noqa: BLE001 -- an instrument never breaks its subject
            return False

    def record(
        self, *, event: str, dispatched: bool = False, path: Optional[str] = None
    ) -> None:
        """Count one inbound request AT THE REQUEST LINE. `dispatched` is
        raised separately once a request has passed routing/validation, so
        received-without-dispatched reads as "the harness dialled and we
        rejected the shape" rather than as a non-dial."""
        first_for_event = False
        try:
            now_iso = _dial_now_iso()
            with self._lock:
                slot = self.events.setdefault(event, {"received": 0, "dispatched": 0})
                if dispatched:
                    slot["dispatched"] += 1
                else:
                    first_for_event = slot["received"] == 0
                    slot["received"] += 1
                    self.last_received_at = now_iso
                    self.tail.append({"event": event, "at": now_iso, "path": path})
                    if len(self.tail) > DIAL_TAIL_MAX:
                        del self.tail[: len(self.tail) - DIAL_TAIL_MAX]
            due = (time.time() - self._last_flush_at) >= DIAL_FLUSH_MIN_INTERVAL_SECS
            if first_for_event or due:
                self.flush()
        except Exception:  # noqa: BLE001 -- never raises into the request path
            pass


class _FrontDoorContext:
    """Boot-scoped process state: the in-flight counter, and the shutdown
    wiring `warm.lifecycle` needs. Mirrors `supervisor._ServerContext`'s
    shape at the scale this module currently needs -- NO op dispatch is
    wired here (module section's own negative-spec: routing is C4's job),
    so `in_flight` currently only ever sees zero, but the wiring is in place
    for C4 to reach through the same closure `supervisor._make_handler`
    demonstrates, rather than a second shutdown shape being invented then.
    """

    def __init__(self, *, httpd: Any, engine_root: Optional[Path]) -> None:
        self.httpd = httpd
        self.engine_root = engine_root
        self.in_flight = InFlightCounter()
        self.dials = DialCounter(engine_root=engine_root)
        self.door_key = self._boot_credential()

    @staticmethod
    def _boot_credential() -> Optional[str]:
        """Read the door's shared secret ONCE, at boot (AC17).

        Read here rather than per fire because this process is resident: a
        per-fire read would put disk I/O back on a path whose whole appeal is
        60 nanoseconds of `compare_digest`.

        The directory boundary is ASSERTED, not assumed -- a secret in a
        directory other users can read is not a secret, and a credential
        believed to be sound is worse than none. A failed assertion does not
        raise out of here: this door must still bind and still answer, because
        `unroutable_response`'s loud did-not-run is a far better outcome than a
        port nothing is listening on. It simply holds no key, so every fire
        answers `credential_absent` until an operator fixes the directory.
        """
        try:
            door_credential.assert_directory_excludes_others()
        except Exception as exc:  # noqa: BLE001 -- never brick the door
            print(
                "[front-door] refusing to hold a credential: %s" % exc,
                file=sys.stderr,
            )
            return None
        return door_credential.read_secret()

    def close_listener(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001 -- best-effort, mirrors supervisor's own contract
            pass

    def ctx_shutdown(self) -> None:
        unlink_discovery(self.engine_root)

    def stop(self) -> None:
        lifecycle.begin_shutdown(
            close_listener=self.close_listener,
            in_flight_count=self.in_flight,
            ctx_shutdown=self.ctx_shutdown,
        )


def _make_handler(ctx: "_FrontDoorContext"):
    """Build a `BaseHTTPRequestHandler` subclass bound to `ctx` via closure
    -- `http.server`'s own idiom, mirrors `supervisor._make_handler`. Only
    `GET <HEALTH_PATH>` is served (this section's own negative-spec: no
    `/hook` routing here, that is C4's `front_door_routing.py`)."""
    from http.server import BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Silence stdlib's default stderr access log -- this process has
            # no operator watching its console, same as `supervisor`'s.
            pass

        def handle_one_request(self) -> None:
            """Count AT THE REQUEST LINE, before routing or validation.

            Placed here rather than in `do_GET`/`do_POST` deliberately: those
            run only after the stdlib has parsed and dispatched by method, so
            a request with a shape we reject would never reach them and would
            be indistinguishable from a request that never arrived. That
            conflation is the exact bug this counter exists to detect, so the
            increment sits ahead of every branch that could swallow it.
            """
            super().handle_one_request()
            try:
                if self.requestline:
                    # STRICT, and deliberately not a fallback to the path.
                    # `doe-claude-a9` hit the general form of this on their own
                    # forwarder: their `_extract_hook_event_name` defaults an
                    # unparsable body to "PreToolUse", which is right for the
                    # deny it shapes and fatal for counting -- a garbage
                    # arrival files itself as an ordinary PreToolUse fire and
                    # the arrived-but-unlabelled population vanishes into the
                    # exact bucket under suspicion. An earlier revision here
                    # had the weaker version: a path-inferred key landed in the
                    # SAME namespace as a header-supplied one, with nothing
                    # marking which was which. A key must mean one thing.
                    labelled = self.headers.get(HOOK_EVENT_HEADER)
                    event = labelled if labelled else UNLABELLED_EVENT
                    ctx.dials.record(event=event, path=self.path)
            except Exception:  # noqa: BLE001 -- an instrument never breaks its subject
                pass

        def do_GET(self) -> None:  # noqa: N802 -- stdlib-mandated name
            if self.path.rstrip("/") == HEALTH_PATH:
                body = json.dumps(door_health_payload(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def _answer(self, payload: Dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 -- stdlib-mandated name
            """Authenticate, resolve, forward -- in that order, and the order
            is the point (AC17).

            The credential is checked BEFORE the route is resolved and before
            anything is forwarded, so an unauthenticated fire never causes this
            process to touch a listener on its behalf. It is never forwarded
            tokenless "in the hope the listener accepts it", and this door
            still mints and recomputes nothing: it COMPARES a shared secret,
            which is a different act from computing an engine token
            (`door_credential`'s own negative spec).

            Every failure answers through `unroutable_response`'s loud
            did-not-run shape rather than an HTTP error status, because a hook
            caller reads the BODY -- a bare 4xx reaches the harness as a guard
            that produced nothing, which is indistinguishable from a guard that
            passed. That indistinguishability is the whole thing this plan
            exists to remove.
            """
            event = self.headers.get(HOOK_EVENT_HEADER) or UNLABELLED_EVENT
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""

            presented = door_credential.credential_from_headers(self.headers)
            if presented is None:
                state = front_door_routing.CREDENTIAL_ABSENT
            elif not door_credential.verify(presented, ctx.door_key):
                state = front_door_routing.CREDENTIAL_INVALID
            else:
                state = None
            if state is not None:
                self._answer(
                    front_door_routing.unroutable_response(
                        front_door_routing.RouteResolution(state=state), event
                    )
                )
                return

            resolution = front_door_routing.resolve_route(
                self.headers, front_door_root=ctx.engine_root
            )
            if resolution.state != front_door_routing.ROUTED:
                self._answer(front_door_routing.unroutable_response(resolution, event))
                return

            # `enter`/`exit`, not `with`: `InFlightCounter` exposes those
            # names rather than the context-manager protocol, and this is the
            # counter `warm.lifecycle` polls to know a drain has finished.
            ctx.in_flight.enter()
            try:
                forwarded = front_door_routing.forward_request(
                    resolution, self.path, body
                )
            finally:
                ctx.in_flight.exit()
            if forwarded is None:
                # The route resolved but the hop did not land. Reported as the
                # no-listener fact rather than a fifth vocabulary: from the
                # caller's side "the record said live and the dial failed" and
                # "no record" are the same remediation, and `forward_request`'s
                # own contract already collapses every hop failure to `None`.
                self._answer(
                    front_door_routing.unroutable_response(
                        front_door_routing.RouteResolution(
                            state=front_door_routing.NO_LISTENER,
                            identity=resolution.identity,
                            engine_root=resolution.engine_root,
                        ),
                        event,
                    )
                )
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(forwarded)))
            self.end_headers()
            self.wfile.write(forwarded)

    return _Handler


def main() -> int:
    """The front-door process entrypoint `ensure_front_door`'s spawn
    trigger targets. Boot sequence, mirroring `supervisor.main`'s numbered
    steps but with the election collapsed onto C2's bind-is-the-election
    primitive rather than a named-pipe lock:

    1. `elect_front_door()` -- bind the fixed port, or discriminate why not
       (module docstring's three typed outcomes). `ElectionLost` means
       another instance of this same door already won this generation;
       exits 0, touches nothing. `ForeignHolderError` is surfaced loudly
       (AC4: never silently swallowed) and exits nonzero. `UnstampedRootYield`
       means this tree is not a real engine; exits 0, touches nothing,
       mirroring `supervisor.ensure_listener`'s own `is_engine_root` gate.
    2. Adopt the already-bound, already-listening socket into a threading
       HTTP server -- NOT a fresh bind (module docstring's "THE ELECTION IS
       THE BIND" section: a second bind here would be a second, redundant
       contention for the same port the election already resolved).
    3. Write the discovery record (port, pid, birth epoch, generation sha,
       door protocol version) -- only reachable past step 1, so a process
       that lost the election never clobbers the winner's record.
    4. Serve forever until `_FrontDoorContext.stop()` (bound to
       `lifecycle.begin_shutdown`) ends the process.
    """
    import sys as _sys

    root = current_engine_clone()
    try:
        sock = elect_front_door(engine_root=root)
    except ElectionLost:
        print(
            f"[front-door] election lost for port {FIXED_PORT}; another "
            "instance of this front door already won, exiting",
            file=_sys.stderr,
        )
        return 0
    except UnstampedRootYield:
        print(
            f"[front-door] engine root {root} is unstamped; yielding rather "
            "than serving",
            file=_sys.stderr,
        )
        return 0
    except ForeignHolderError as exc:
        print(f"[front-door] {exc}", file=_sys.stderr)
        return 1

    from http.server import ThreadingHTTPServer

    class _NotYetBound:
        pass

    # `bind_and_activate=False` -- the socket ThreadingHTTPServer's own
    # `__init__` would otherwise create is a throwaway; `sock` is already
    # bound AND listening (C2's `elect_front_door`), and re-binding here
    # would be the redundant second contention this module's docstring
    # rules out.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NotYetBound, bind_and_activate=False)
    httpd.socket.close()
    httpd.socket = sock
    httpd.server_address = sock.getsockname()

    ctx = _FrontDoorContext(httpd=httpd, engine_root=root)
    httpd.RequestHandlerClass = _make_handler(ctx)

    # Publish the dial counter AT ZERO before serving. This is what makes
    # "absent" and "zero" different readings: absent means this door never
    # started and says nothing about dialling, while a zero stamped with
    # `boot_at` means the door has been up since then and the harness has
    # NEVER DIALLED. Written before `serve_forever` so no request can ever
    # land ahead of the baseline it is counted against.
    ctx.dials.flush()

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
        print(f"[front-door] failed to write discovery record: {exc!r}", file=_sys.stderr)

    try:
        httpd.serve_forever()
    finally:
        ctx.ctx_shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
