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
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.warm.engine_root import current_engine_clone

from coordinator_core import locked_write
from coordinator_core.session.core import stable_pid_alive
from coordinator_core.warm import breadcrumb, election, lifecycle, skew
from coordinator_core.warm.server import InFlightCounter

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
HOOK_PATH = "/hook"

# A liveness probe, not a work budget -- mirrors `warm.client.
# READ_DEADLINE_SECS`'s "is the server wedged" framing, sized the same
# (2.0s) since both ask the identical question of a sibling transport.
HEALTH_CHECK_TIMEOUT_SECS = 2.0

# Reuses `breadcrumb.SPAWN_DEBOUNCE_SECS` rather than defining a second
# debounce window that could drift from the pipe transport's.
SPAWN_DEBOUNCE_SECS = breadcrumb.SPAWN_DEBOUNCE_SECS

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

    with locked_write.held_lock(path, holder_label="warm.supervisor"):
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8", newline="\n")


def read_discovery(engine_root: Optional[Path] = None) -> Optional[dict]:
    """Read and parse the discovery record, or return None if absent,
    unreadable, or not a well-formed JSON object -- never raises, mirrors
    `breadcrumb.read_breadcrumb`'s HINT contract: every consumer must treat
    `None` as "no information," not an error."""
    path = discovery_path(engine_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return record


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

    Never raises: every read/health-check primitive it calls already has
    a "never raises, degrade to None/False" contract, and this function
    adds no unguarded call of its own.
    """
    root = engine_root if engine_root is not None else _default_engine_clone()
    try:
        record = read_discovery(root)
        if record is not None and discovery_is_live(record):
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
    needs (no idle watchdog, no skew eviction -- those are pipe-transport
    concerns this row does not extend to http; a follow-up chunk's job if
    ever asked for).
    """

    def __init__(self, *, httpd: Any, engine_root: Optional[Path]) -> None:
        self.httpd = httpd
        self.engine_root = engine_root
        self.in_flight = InFlightCounter()

    def close_listener(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001 -- best-effort, mirrors ctx_shutdown's own contract
            pass

    def ctx_shutdown(self) -> None:
        unlink_discovery(self.engine_root)

    def stop(self) -> None:
        lifecycle.begin_shutdown(
            close_listener=self.close_listener,
            in_flight_count=self.in_flight,
            ctx_shutdown=self.ctx_shutdown,
        )


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

        def do_POST(self) -> None:  # noqa: N802 -- stdlib-mandated name
            if self.path.rstrip("/") != HOOK_PATH:
                self.send_response(404)
                self.end_headers()
                return

            ctx.in_flight.enter()
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}

                # WIRING NOTE (module docstring): this seam does not
                # translate real hook semantics -- it answers a default
                # "allow", present and health-checked, never a hang or an
                # error a fired hook would have to fail open against.
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": payload.get("hook_event_name"),
                        "permissionDecision": "allow",
                    }
                }
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                ctx.in_flight.exit()

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
    3. Write the discovery record (port, pid, birth epoch, generation sha)
       -- only reachable past step 1, so a process that lost the election
       never clobbers the winner's record.
    4. Serve forever until `lifecycle.begin_shutdown` (bound to `_stop`)
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
    ctx = _ServerContext(httpd=httpd, engine_root=root)
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
        httpd.serve_forever()
    finally:
        ctx.ctx_shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
