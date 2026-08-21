"""The POSIX door's read is BOUNDED, and its bound never re-runs a delivered request.

> **THIS TEST HAS NEVER BEEN RUN, ON ANY PLATFORM.** It was authored on
> Windows, where it cannot execute (it skips itself there) and where the
> binary it exercises cannot be built. It is the macOS/Linux twin of
> `test_door_read_deadline.py` and is meant to be the FIRST thing a Mac user
> runs after `make check` -- see `coordinator_core/warm/door/README-posix.md`.
> Expect it to need fixing before it passes; a green run is the deliverable,
> not this file.

WHY A SEPARATE MODULE FROM `test_door_read_deadline.py`. Its stubs are
`_winapi` named pipes end to end (`election.elect`, `ConnectNamedPipe`,
`_winapi.ReadFile`), and `election.elect()` raises off Windows by design.
There is no shared fixture to factor out -- only a shared SHAPE, which this
module follows deliberately so the two read as the same test.

THE TWO PROPERTIES, and they pull in opposite directions -- a test asserting
only the first would happily pass on a "fix" that reintroduces the
2026-08-19 double-commit incident:

  1. TERMINATION. A wedged server must not hold the door open forever. The
     door stops at `DOOR_READ_DEADLINE_MS`, read here out of `door_posix.c`
     itself rather than pinned as a literal, so the assertion tracks the
     source it is about instead of drifting from it.

  2. NON-RE-EXECUTION. Stopping must NOT become falling through. Once the
     request has been fully written the server may already be executing it,
     and re-running that cold double-executes a mutation. The deadline's
     expiry is therefore the `-32004` envelope and a nonzero exit -- the
     door's existing post-delivery behaviour reached by one more route,
     never a third behaviour.

THE NEGATIVE CONTROL IS THE POINT. `test_pre_delivery_failure...` asserts the
fallback marker IS present before `test_wedged_server...` asserts it is
ABSENT. Without the first, the second proves nothing: an absent marker is
equally consistent with a fixture that could never have produced one.

THE SOCKET PATH IS ASKED FOR, NOT MIRRORED. `_socket_path_for()` below
calls `election.socket_path` -- the same helper `server._elect_unix_socket_
endpoint` binds -- so the door and its peer cannot pick different paths and
both stay green. It derived the path by hand until 2026-08-21, when no
production POSIX binder existed to ask (`server.py::main()` refused to start
off Windows and `election.py` had no POSIX branch); that was the shape
`state/lessons/2026-08-05-coverage-that-builds-its-own-input-never-tests-the-
door-production-uses.yaml` warns about, named here rather than hidden, and it
is now closed. The recipe still lives in exactly one place -- if it moves
again, it moves for both halves at once.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name == "nt", reason="door_posix is a macOS/Linux binary"),
]

_DOOR_DIR = Path(__file__).resolve().parents[1] / "door"
_DOOR_BIN = _DOOR_DIR / "door"
_DOOR_SOURCE = _DOOR_DIR / "door_posix.c"

#: Printed by the stub `coordinator-invoke.py` the door falls through to.
#: Its presence is proof of a fallback spawn; its absence, proof of a refusal.
_FALLBACK_MARKER = "DOOR-TEST-FALLBACK-RAN"
_FALLBACK_EXIT = 42


def _require_binary() -> None:
    """The POSIX door is not committed as a binary the way `door.exe` is, so
    a missing one is 'you have not built it yet', not a failure."""
    if not _DOOR_BIN.exists():
        pytest.skip(
            f"no door binary at {_DOOR_BIN} -- build it first: "
            "python3 -m coordinator_core.warm.door.build_posix <published-engine-root>"
        )


def _door_deadline_ms(macro: str) -> int:
    """Reads a deadline macro's value out of `door_posix.c`, so this file
    asserts against the number the binary was built from rather than a literal
    that can silently drift away from it."""
    source = _DOOR_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", source, re.MULTILINE)
    assert match, f"{macro} not found in {_DOOR_SOURCE} -- renamed or removed"
    return int(match.group(1))


def _make_stub_engine_root(tmp_path: Path) -> Path:
    """A throwaway directory shaped enough like a published engine for the
    door to accept it: a non-empty `coordinator_core/_engine_stamp` (what
    `is_valid_engine_root` checks) plus a `coordinator/bin/
    coordinator-invoke.py` that announces itself instead of running an op.

    The stamp bytes are unique per run, so `compute_client_token` yields a
    token -- and therefore a socket name -- no other process is using."""
    root = tmp_path / "stub-engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"door-read-deadline-test-{os.getpid()}-{time.time_ns()}\n", encoding="utf-8"
    )
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "coordinator-invoke.py").write_text(
        "import sys\n"
        f"print({_FALLBACK_MARKER!r})\n"
        f"raise SystemExit({_FALLBACK_EXIT})\n",
        encoding="utf-8",
    )
    return root.resolve()


def _socket_path_for(runtime_base: Path, engine_root: Path) -> Path:
    """The path the POSIX server would bind for `engine_root`, from the
    server's own helper.

    `election.socket_path` reads the runtime base out of the environment
    (via `breadcrumb.svc_dir`), which is where `_run_door` puts it for the
    door subprocess too -- so the base is set around the call and restored
    rather than threaded through a second parameter the production helper
    does not have. `door_posix.c` derives the same path in C; that C
    derivation is what these tests exist to check, so nothing here may
    reproduce it."""
    from coordinator_core.warm import breadcrumb, election, skew

    prior = os.environ.get(breadcrumb.RUNTIME_BASE_ENV)
    os.environ[breadcrumb.RUNTIME_BASE_ENV] = str(runtime_base)
    try:
        return election.socket_path(
            skew.compute_client_token(engine_root), engine_clone=engine_root
        )
    finally:
        if prior is None:
            os.environ.pop(breadcrumb.RUNTIME_BASE_ENV, None)
        else:
            os.environ[breadcrumb.RUNTIME_BASE_ENV] = prior


def _make_socket_dir(sock_path: Path) -> None:
    """Creates the socket's containing directory the way the door demands it:
    0700, owned by us, under a parent that is not group/other-writable.

    `chmod` AFTER `mkdir` is not belt-and-braces -- `mkdir`'s mode argument is
    masked by the process umask, so a requested 0700 under a umask of 0
    is still 0700 but under an inherited-odd umask may not be what you
    asked for. The door verifies the RESULTING mode (`dir_is_private`), so
    this fixture must produce the resulting mode too, not merely request it.
    A server that only requests 0700 is the most likely reason a real door
    refuses to connect -- see README-posix.md's expected-failures list."""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(sock_path.parent, 0o700)
    os.chmod(sock_path.parent.parent, 0o755)


class _WedgedServer:
    """A Unix domain socket that accepts exactly one connection and then does
    NOTHING with it -- no read, no reply, no close. This is the shape the
    resident server degrades to when its worker threads are gone: the
    connection is accepted and enqueued, and nothing ever dequeues it."""

    def __init__(self, sock_path: Path) -> None:
        self._path = sock_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(sock_path))
        self._sock.listen(1)
        self._accepted: socket.socket | None = None
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        try:
            self._accepted, _ = self._sock.accept()
            # Deliberately nothing else. Holding the accepted socket open is
            # the whole fixture: closing it would make this a
            # connection-reset test, which is a DIFFERENT post-delivery route.
        except OSError:
            pass

    def close(self) -> None:
        for handle in (self._accepted, self._sock):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._path.unlink(missing_ok=True)


class _ReplyingServer:
    """A Unix domain socket that reads one request frame and answers it
    verbatim with `reply`.

    This is the control the deadline tests need to be worth anything: a bound
    on a read that does not read correctly is not a fix. These exercise the
    ORDINARY outcomes -- a success envelope and a provably-undispatched error
    -- through the bounded mechanism, end to end."""

    def __init__(self, sock_path: Path, reply: bytes) -> None:
        self._path = sock_path
        self._reply = reply
        self.request = b""
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(sock_path))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn = None
        try:
            conn, _ = self._sock.accept()
            while b"\n" not in self.request:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                self.request += chunk
            conn.sendall(self._reply)
        except OSError:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._thread.join(timeout=10)
        try:
            self._sock.close()
        except OSError:
            pass
        self._path.unlink(missing_ok=True)


def _run_door(engine_root: Path, runtime_base: Path, timeout: float):
    """Runs `door ping` pointed at `engine_root` via the documented
    `COORDINATOR_DOOR_ENGINE_ROOT` override, with the runtime base redirected
    into `tmp_path` via `COORDINATOR_WARM_RUNTIME_BASE`.

    Both overrides are deliberate. The engine-root one avoids installing a
    `door.engine-root.txt` sidecar anywhere, which is a separate decision a
    test has no business pre-empting. The runtime-base one keeps this test
    out of the operator's real `~/.cache/coordinator/warm/`, so it can never
    collide with -- or be satisfied by -- a real resident server.

    `timeout` is a harness backstop only: a door that needs it has already
    failed the property under test."""
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    env["COORDINATOR_WARM_RUNTIME_BASE"] = str(runtime_base)
    return subprocess.run(
        [str(_DOOR_BIN), "ping"],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=str(engine_root),
    )


def test_pre_delivery_failure_still_falls_through(tmp_path: Path) -> None:
    """Nothing is listening, so the connect fails BEFORE any byte is written.
    That is ordinary doubt, and the door's answer to ordinary doubt is
    unchanged by the deadline: fall through silently to the Python
    entrypoint, propagate its exit code, print no envelope of its own.

    This also establishes that the fallback is genuinely reachable from this
    fixture -- which is what makes the wedged test's assertion that it did
    NOT fire mean anything at all."""
    _require_binary()
    root = _make_stub_engine_root(tmp_path)
    proc = _run_door(root, tmp_path / "runtime", timeout=60)

    assert _FALLBACK_MARKER in proc.stdout
    assert proc.returncode == _FALLBACK_EXIT
    assert "-32004" not in proc.stdout
    assert "warm dispatch indeterminate" not in proc.stdout


def test_a_served_reply_still_round_trips(tmp_path: Path) -> None:
    """The fast path. A bound on a read that stopped reading correctly is not
    a fix, so this asserts the whole exchange: the request the server received
    is a parseable `invoke.from_argv` frame (proving the bounded WRITE
    delivered it intact), and the door relays the reply's three result
    fields."""
    _require_binary()
    import json

    root = _make_stub_engine_root(tmp_path)
    runtime_base = tmp_path / "runtime"
    sock_path = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock_path)

    reply = (
        '{"jsonrpc":"2.0","id":1,"result":'
        '{"stdout":"pong\\n","stderr":"","exit_code":0}}\n'
    ).encode("utf-8")

    server = _ReplyingServer(sock_path, reply)
    try:
        proc = _run_door(root, runtime_base, timeout=60)
    finally:
        server.close()

    request = json.loads(server.request.decode("utf-8").strip())
    assert request["method"] == "invoke.from_argv"
    assert request["params"]["argv"] == ["ping"]

    assert proc.returncode == 0
    assert proc.stdout == "pong\n"
    assert _FALLBACK_MARKER not in proc.stdout


def test_provably_undispatched_error_still_falls_through(tmp_path: Path) -> None:
    """The one post-delivery route still allowed to fall through: an error
    code `is_provably_undispatched` recognises as proof the server never
    invoked a handler. The port must not have collapsed this into the refusal
    branch."""
    _require_binary()
    root = _make_stub_engine_root(tmp_path)
    runtime_base = tmp_path / "runtime"
    sock_path = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock_path)

    reply = (
        '{"jsonrpc":"2.0","id":1,"error":'
        '{"code":-32601,"message":"Method not found"}}\n'
    ).encode("utf-8")

    server = _ReplyingServer(sock_path, reply)
    try:
        proc = _run_door(root, runtime_base, timeout=60)
    finally:
        server.close()

    assert _FALLBACK_MARKER in proc.stdout
    assert proc.returncode == _FALLBACK_EXIT
    assert "-32004" not in proc.stdout


def test_a_world_readable_socket_dir_is_refused(tmp_path: Path) -> None:
    """POSIX-ONLY PROPERTY, with no Windows counterpart -- and the reason it
    has none is the point. On Windows the pipe's SDDL ACL is enforced by the
    kernel on connect. On POSIX a Unix socket's own mode is NOT reliably
    enforced on connect (macOS and the BSDs), so the containing directory's
    0700 IS the boundary, and the door checks it itself.

    A live, correctly-answering server behind a 0755 directory must therefore
    produce a FALL-THROUGH, not a fast-path success. Getting this wrong is
    silent: everything works, and the boundary is simply gone."""
    _require_binary()
    root = _make_stub_engine_root(tmp_path)
    runtime_base = tmp_path / "runtime"
    sock_path = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock_path)

    reply = (
        '{"jsonrpc":"2.0","id":1,"result":'
        '{"stdout":"pong\\n","stderr":"","exit_code":0}}\n'
    ).encode("utf-8")
    server = _ReplyingServer(sock_path, reply)
    # Widened AFTER the socket exists, so the only variable between this test
    # and `test_a_served_reply_still_round_trips` is the directory's mode.
    os.chmod(sock_path.parent, 0o755)
    try:
        proc = _run_door(root, runtime_base, timeout=60)
    finally:
        server.close()

    assert _FALLBACK_MARKER in proc.stdout, (
        "the door connected through a world-readable socket directory -- on "
        "macOS that directory is the entire access boundary, standing in for "
        "the Windows door's SDDL ACL"
    )
    assert proc.returncode == _FALLBACK_EXIT
    assert proc.stdout.count("pong") == 0


def test_wedged_server_bounds_the_read_and_refuses_to_re_run(tmp_path: Path) -> None:
    """The server accepts and never answers. The door must stop -- and must
    stop by REFUSING, never by re-running a request it already delivered.

    COST, NAMED: this waits out the real 40s deadline rather than a shortened
    one, because the deadline VALUE is half the property. A door that gave up
    in 200ms would pass a termination-only test while manufacturing `-32004`
    refusals for every op the server was going to answer. One process blocked
    in `poll()`, burning no CPU -- but 40s of wall clock, which is why this
    module is `cadence`-tiered and off the per-commit path."""
    _require_binary()
    root = _make_stub_engine_root(tmp_path)
    runtime_base = tmp_path / "runtime"
    sock_path = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock_path)

    deadline_s = _door_deadline_ms("DOOR_READ_DEADLINE_MS") / 1000.0

    server = _WedgedServer(sock_path)
    try:
        started = time.monotonic()
        # The backstop is generously above the deadline: it exists to fail the
        # test rather than hang the suite, and must never be the thing that
        # produces the "returned in time" verdict.
        proc = _run_door(root, runtime_base, timeout=deadline_s * 3)
        elapsed = time.monotonic() - started
    finally:
        server.close()

    # Property 1 -- it terminated, at the deadline it claims.
    assert elapsed < deadline_s * 2, (
        f"the door took {elapsed:.1f}s against a {deadline_s:.0f}s read deadline"
    )
    assert elapsed >= deadline_s * 0.8, (
        f"the door gave up after {elapsed:.1f}s, well inside its {deadline_s:.0f}s "
        "deadline -- a door that abandons a delivered request early manufactures "
        "-32004 refusals for ops the server was going to answer"
    )

    # Property 2 -- it refused, in the ONE post-delivery shape the door
    # already had. No third behaviour.
    assert proc.returncode != 0
    assert '"code":-32004' in proc.stdout
    assert "warm dispatch indeterminate" in proc.stdout

    # Property 2, the half that actually guards the 2026-08-19 incident: the
    # delivered request was NOT re-run cold.
    assert _FALLBACK_MARKER not in proc.stdout, (
        "the door fell through AFTER delivering the request -- this is the "
        "double-execution the -32004 envelope exists to prevent"
    )
