"""Tests for `coordinator_core.warm.server`'s POSIX unix-socket arm.

TIERED THE SAME WAY `test_election_posix.py` IS, for the same reason. The
POSIX server differs from the Windows one in exactly two places -- the boot
election and the accept layer -- and everything below the enqueue point is
one shared implementation. So:

  - Tier 1, runs everywhere (including Windows): the accept layer's own
    decision machine (`_acceptor_loop`) against a fake listening socket, the
    connection wrapper's lifetime contract (`_wrap_socket`, driven by a real
    `socket.socketpair()` -- which Windows provides), the shutdown-path
    socket unlink, and the boot branch's platform routing. No AF_UNIX
    anywhere in this tier.
  - Tier 2, POSIX kernel required and SKIPPED WITH A REASON off it: a real
    server bound to a real unix socket, served end to end, plus the
    abandoned-client (EPIPE) case that the Windows arm's own load-bearing
    worker guard exists for.

The shared machinery below the enqueue is NOT re-tested here.
`test_server_loop.py::test_worker_loop_survives_an_unhandled_exception_from
_handle_connection` already pins the worker-thread survival guard, and it is
transport-agnostic -- the POSIX arm inherits it by running the same
`_worker_loop`, not by having its own copy. What this file adds for that
guard is Tier 2's end-to-end proof that a unix-socket EPIPE really does land
where the guard catches it.

WHAT IS NOT PROVEN HERE, stated because a skipped test looks like a passing
one from a distance: as of 2026-08-21 the Tier-2 tests below have never
been executed. They were authored on Windows, where the entire POSIX server
path is unreachable.
"""

from __future__ import annotations

import inspect
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, election, idle, lifecycle, server, skew

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs a POSIX kernel: the server's AF_UNIX bind/accept path does not exist on Windows",
)


@pytest.fixture(autouse=True)
def _reset_shutdown_guard():
    """Mirrors `test_server_loop.py`'s own fixture -- `warm.lifecycle`'s
    shutdown guard and `warm.idle`'s clock are both process-global."""
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    yield
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


class _FakeVersionState:
    def __init__(self, *, skewed: bool = False, server_sha: str = "deadbeef"):
        self._skewed = skewed
        self.server_sha = server_sha

    def is_skewed(self, client_token: str) -> bool:
        return self._skewed


class _FakeListener:
    """A listening socket's `accept()` and nothing else -- enough to drive
    `_acceptor_loop`'s decisions without an AF_UNIX kernel path."""

    def __init__(self, results):
        self._results = list(results)
        self.accept_calls = 0

    def accept(self):
        self.accept_calls += 1
        if not self._results:
            raise OSError("listener closed")
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, "peer"


class _FakeConn:
    def __init__(self):
        self.closed = False

    def makefile(self, mode):
        return f"io-for-{id(self)}"

    def close(self):
        self.closed = True


def _ctx(**kwargs):
    kwargs.setdefault("name", "/run/u/coordinator/warm/hash/tok.sock")
    kwargs.setdefault("sid", "501")
    kwargs.setdefault("version_state", _FakeVersionState())
    return server._ServerContext(**kwargs)


# ---------------------------------------------------------------------------
# Tier 1 -- runs on every platform, Windows included.
# ---------------------------------------------------------------------------


def test_acceptor_enqueues_every_accepted_connection(monkeypatch) -> None:
    """The acceptor's whole job: take the connection off the kernel and put
    it on the shared queue. Dispatch happens on a bounded worker, never
    here -- that split is what keeps acceptance from spawning an unbounded
    number of dispatching threads (AC7)."""
    monkeypatch.setattr(server, "_wrap_socket", lambda conn: f"io-{id(conn)}")
    ctx = _ctx()
    conns = [_FakeConn(), _FakeConn()]

    ctx._acceptor_loop(_FakeListener(conns))

    assert ctx._queue.qsize() == 2
    assert ctx.in_flight() == 2  # every enqueue claims its slot at the enqueue point


def test_acceptor_ends_when_the_listening_socket_closes(monkeypatch) -> None:
    """`accept()` raising OSError means the listening socket is gone --
    `_ctx_shutdown`, microseconds before `os._exit(0)`. Retrying would spin
    a doomed thread against a dead fd."""
    monkeypatch.setattr(server, "_wrap_socket", lambda conn: "io")
    ctx = _ctx()
    listener = _FakeListener([])

    ctx._acceptor_loop(listener)

    assert listener.accept_calls == 1
    assert ctx._queue.qsize() == 0


def test_acceptor_drops_a_connection_arriving_after_close_listener(monkeypatch) -> None:
    """Same outcome the Windows accept chain produces for an instance
    connected after the listener closed: the client reads EOF and goes cold
    rather than being answered by a draining generation."""
    monkeypatch.setattr(server, "_wrap_socket", lambda conn: "io")
    ctx = _ctx()
    ctx.close_listener()
    conn = _FakeConn()

    ctx._acceptor_loop(_FakeListener([conn]))

    assert conn.closed is True
    assert ctx._queue.qsize() == 0


def test_acceptor_survives_a_wrap_failure_and_keeps_accepting(monkeypatch) -> None:
    """The acceptor-side analog of `_worker_loop`'s load-bearing guard: one
    bad connection must cost that connection, never this thread. A dead
    acceptor is a permanent capacity loss."""
    calls = {"n": 0}

    def _flaky(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated wrap failure")
        return f"io-{calls['n']}"

    monkeypatch.setattr(server, "_wrap_socket", _flaky)
    ctx = _ctx()
    bad, good = _FakeConn(), _FakeConn()

    ctx._acceptor_loop(_FakeListener([bad, good]))

    assert bad.closed is True
    assert ctx._queue.qsize() == 1  # the good one still got through


def test_wrap_socket_keeps_the_connection_open_until_the_file_object_closes() -> None:
    """`_wrap_socket` closes the socket object immediately after `makefile`.
    That is the documented CPython idiom, not a leak -- but if it were
    wrong, every served request would die before it could be answered.
    Driven on a real socket pair, which Windows provides for AF_INET.
    """
    left, right = socket.socketpair()
    try:
        io = server._wrap_socket(right)

        left.sendall(b"hello\n")
        assert io.readline() == b"hello\n"

        io.write(b"world\n")
        io.flush()
        assert left.recv(6) == b"world\n"

        io.close()
    finally:
        left.close()


def test_wrap_socket_release_is_complete_when_the_file_object_closes() -> None:
    """The other half of the same contract: once `_handle_connection`'s
    `finally` closes the io object, the peer must see EOF. A retained
    reference here would keep every served connection half-open for the life
    of a resident server."""
    left, right = socket.socketpair()
    try:
        io = server._wrap_socket(right)
        io.close()
        assert left.recv(16) == b""
    finally:
        left.close()


def test_ctx_shutdown_removes_only_its_own_socket_file(tmp_path, monkeypatch) -> None:
    """A superseded generation exiting must not delete its live successor's
    endpoint -- the same hazard `breadcrumb.unlink_breadcrumb`'s `owner_pid`
    check closes, one artifact over."""
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path))
    endpoint = tmp_path / "tok.sock"
    endpoint.write_bytes(b"")

    ctx = _ctx(engine_root=tmp_path, endpoint_path=endpoint)

    # A successor replaced the file between this server's bind and its exit.
    endpoint.unlink()
    endpoint.write_bytes(b"successor")

    ctx._ctx_shutdown()
    assert endpoint.exists(), "a departing generation deleted its successor's socket"


def test_ctx_shutdown_removes_the_socket_it_actually_bound(tmp_path, monkeypatch) -> None:
    """The corpse this removes is the one every future `bind()` would fail
    EADDRINUSE against (`warm.election`'s module docstring)."""
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path))
    endpoint = tmp_path / "tok.sock"
    endpoint.write_bytes(b"")

    ctx = _ctx(engine_root=tmp_path, endpoint_path=endpoint)
    ctx._ctx_shutdown()

    assert not endpoint.exists()


def test_ctx_shutdown_on_the_windows_shape_touches_no_socket(tmp_path, monkeypatch) -> None:
    """Anti-regression pin. Every pre-existing `_ServerContext(...)` omits
    both new fields, and must keep exactly its old shutdown behaviour."""
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path))
    ctx = _ctx(engine_root=tmp_path)

    assert ctx.listen_socket is None
    assert ctx.endpoint_path is None
    assert ctx._endpoint_identity is None
    ctx._ctx_shutdown()  # must not raise


def test_boot_routes_to_exactly_one_election_per_platform() -> None:
    """One sequence, two transports: the ONLY platform branch in the boot
    path is which election runs. A second branch appearing anywhere else in
    `_run_guarded` is the start of two servers that drift apart."""
    source = inspect.getsource(server._run_guarded)
    assert "_elect_windows_pipe" in source
    assert "_elect_unix_socket_endpoint" in source
    assert source.count("sys.platform") == 1, "the platform is read once, at the election"


def test_the_windows_election_arm_is_unchanged_in_substance() -> None:
    """The Windows arm was lifted verbatim when the POSIX arm landed. These
    three calls, in this order, are the whole of what it does -- and they
    are what the working platform's behaviour depends on."""
    source = inspect.getsource(server._elect_windows_pipe)
    assert source.index("current_user_sid()") < source.index("election.pipe_name(token")
    assert source.index("election.pipe_name(token") < source.index("election.elect(name")


def test_the_socket_path_helper_is_the_one_production_derivation(monkeypatch) -> None:
    """CONTRACT PIN. `election.socket_path` is the single production
    derivation of the POSIX endpoint; the boot path calls it and nothing
    recomputes the shape locally. A test (or a door) that derives its own
    is coverage building its own input -- it passes while the two halves
    connect to different paths and every surface stays green.
    """
    boot = inspect.getsource(server._elect_unix_socket_endpoint)
    assert "election.socket_path(token, engine_clone=repo_root)" in boot
    # No hand-spelled suffix LITERAL (the `election.socket_path` call itself
    # contains the substring, so the check is for a quoted one).
    assert '".sock"' not in boot and "'.sock'" not in boot

    from coordinator_core.warm import breadcrumb, election

    # A short notional base: the conftest home-quarantine sets a ~90-char
    # real one, which trips the sun_path budget and turns this derivation
    # check into a failure about the fixture. Nothing is touched on disk.
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, "/run/u")
    derived = election.socket_path("tok1", engine_clone=Path.cwd())
    assert derived.parent == breadcrumb.svc_dir(Path.cwd())
    assert derived.name == "tok1.sock"


def test_posix_boot_uses_the_breadcrumbs_transport_field() -> None:
    """The breadcrumb must say which kind of endpoint it is carrying, so a
    reader names it rather than sniffing the string."""
    source = inspect.getsource(server._run_guarded)
    assert "transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX" in source


def test_main_no_longer_refuses_to_run_off_windows() -> None:
    """The blocker this whole arm exists to remove. `main()` returned 1 with
    "this module is Windows-only" until 2026-08-21, which is why a POSIX
    door had no server to talk to."""
    source = inspect.getsource(server.main)
    assert "Windows-only" not in source


def test_each_election_arm_fills_exactly_one_transport_slot() -> None:
    """One slot per transport, one filled. Driven against the real return
    values rather than the source: `_elect_windows_pipe` is exercised for
    real on Windows, and both arms are checked for the invariant the serve
    dispatch depends on."""
    if sys.platform == "win32":
        from coordinator_core.warm import election, skew

        token = "0123456789abcdef"
        elected = server._elect_windows_pipe(Path.cwd(), token)
        try:
            assert isinstance(elected.first_handle, int)
            assert elected.listen_socket is None
            assert elected.endpoint_path is None
            assert elected.endpoint.startswith(r"\\.\pipe\coordinator-core.")
            assert elected.identity == election.current_user_sid()
        finally:
            import _winapi

            _winapi.CloseHandle(elected.first_handle)
        assert skew is not None  # import pinned: the arms share one token source


def test_the_serve_dispatch_narrows_on_the_endpoint_not_the_platform() -> None:
    """The value that decides is the value that gets passed, so the
    not-None check IS the narrowing. Pinned because the previous shape
    re-read `on_windows` at the serve site, where a static reader could
    only see `Optional[int]` reaching a method typed `int` -- correct at
    runtime, unprovable by inspection, and exactly the ambiguity a later
    edit resolves the wrong way.
    """
    boot = inspect.getsource(server._run_guarded)
    assert "if elected.first_handle is not None:" in boot
    assert "elif elected.listen_socket is not None:" in boot
    # An election that won neither must die audibly, not serve nothing.
    assert "raise election.ElectionError(" in boot


def test_elected_is_a_named_tuple_so_the_slots_cannot_transpose() -> None:
    """A bare 5-tuple read identically for both arms while meaning
    opposite things in two slots."""
    assert server._Elected._fields == (
        "endpoint",
        "identity",
        "first_handle",
        "listen_socket",
        "endpoint_path",
    )


def test_the_dispatch_runaway_guard_is_not_transport_owned() -> None:
    """The door derives `DOOR_READ_DEADLINE_MS` (40s) from this server's own
    runaway guard: `ipc.DISPATCH_TIMEOUT_SECS` (30s) plus
    `cc_invoke._op_timeout_ceiling`'s 10s margin. That derivation is only
    sound if the guard lives BELOW the transport -- one `asyncio.wait_for`
    inside `ipc`, reached identically by both platforms and by every
    dispatch-pool worker. A POSIX-side copy, or a transport-level timeout
    here, would be a second number free to disagree with the door's.
    """
    from coordinator_core import ipc

    assert ipc.DISPATCH_TIMEOUT_SECS == 30.0

    # Neither the shared dispatch seam nor either accept layer may own a
    # timeout of its own.
    for fn in (server._run_dispatch, server._ServerContext._acceptor_loop):
        assert "wait_for" not in inspect.getsource(fn)
        assert "settimeout" not in inspect.getsource(fn)


def test_the_posix_accept_layer_reuses_the_shared_worker_pool() -> None:
    """The property that keeps the POSIX arm from becoming a second server:
    everything below the enqueue point is the same code."""
    source = inspect.getsource(server._ServerContext.serve_forever_unix)
    assert "self._start_worker_pool()" in source
    assert "self._idle_watchdog_loop" in source
    assert "self._stopped.wait()" in source


# ---------------------------------------------------------------------------
# Tier 2 -- real POSIX syscalls. Skipped, with a reason, off a POSIX kernel.
# ---------------------------------------------------------------------------


def _serve_in_background(ctx, listen_socket):
    thread = threading.Thread(
        target=lambda: ctx._start_acceptor_pool(listen_socket, pool_size=2), daemon=True
    )
    thread.start()
    return thread

@pytest.fixture()
def short_tmp_path():
    """The suite-root warm-runtime base, used here as an ENGINE ROOT too.

    `coordinator_core/conftest.py::_quarantine_real_home` already redirects
    `breadcrumb.RUNTIME_BASE_ENV` to a short, real, per-test root under
    `/tmp` on POSIX (removed on that fixture's own teardown) -- the fix for
    exactly the `sun_path` overflow this fixture used to work around by
    minting a second short tempdir. These four tests reuse that SAME base as
    their engine root rather than deriving a fresh one, and add the one thing
    the suite-root fixture does not provide: a build stamp
    (`skew.compute_client_token` refuses an unstamped root, the same refusal
    a real caller meets, so this satisfies it the same way rather than
    weakening the check).
    """
    base = Path(os.environ[breadcrumb.RUNTIME_BASE_ENV])
    stamp = base / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("test-engine-stamp\n", encoding="utf-8")
    return base



@posix_only
def test_a_real_unix_socket_server_answers_a_real_client(short_tmp_path, monkeypatch) -> None:
    """The end-to-end shape a Mac user's door will meet: elect a socket,
    accept on it, read one framed request, dispatch, write one framed
    response, close.

    `_pool_dispatch` is replaced by an in-process echo so this test proves
    the TRANSPORT, not the dispatch process pool (which
    `test_dispatch_concurrency.py` owns and which would cost real process
    spawns here).
    """
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(short_tmp_path))
    path = short_tmp_path / "svc" / "tok.sock"
    listen_socket = election.elect_unix_socket(path)

    ctx = _ctx(name=str(path), engine_root=short_tmp_path, listen_socket=listen_socket, endpoint_path=path)
    monkeypatch.setattr(
        type(ctx),
        "_pool_dispatch",
        lambda self, msg, *, caller=None, isolated=False: {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {"echo": msg["method"]},
        },
    )
    ctx._start_worker_pool(pool_size=2)
    _serve_in_background(ctx, listen_socket)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(str(path))
        # STAMPED, because the server refuses an unstamped request before it
        # ever reaches dispatch (-32003, "carried no _engine_token"). This
        # test could not have known that: it has never executed, so it still
        # carried the pre-guard request shape. `skew.compute_client_token` is
        # what `warm.client` stamps, named by the refusal message itself.
        request = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "ping",
            "params": {},
            "_engine_token": skew.compute_client_token(short_tmp_path),
        }
        client.sendall((json.dumps(request) + "\n").encode("utf-8"))
        reader = client.makefile("rb")
        response = json.loads(reader.readline())
    finally:
        client.close()
        listen_socket.close()

    assert response["id"] == "req-1"
    assert response["result"] == {"echo": "ping"}


@posix_only
def test_an_abandoned_client_does_not_kill_a_worker(short_tmp_path, monkeypatch) -> None:
    """The unix-socket form of the failure the Windows arm was losing 30
    workers a session to: a client that hit its own read deadline and closed
    the connection makes the server's response write raise EPIPE. That must
    cost the response, never the worker thread.

    Proven by serving a SECOND request after the first client abandons: if
    the guard is gone, the pool has one fewer worker and (with pool_size=1)
    the second request is never answered at all.
    """
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(short_tmp_path))
    path = short_tmp_path / "svc" / "tok.sock"
    listen_socket = election.elect_unix_socket(path)

    release = threading.Event()

    ctx = _ctx(name=str(path), engine_root=short_tmp_path, listen_socket=listen_socket, endpoint_path=path)

    def _slow_dispatch(self, msg, *, caller=None, isolated=False):
        if msg["id"] == "abandoned":
            release.wait(5)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    monkeypatch.setattr(type(ctx), "_pool_dispatch", _slow_dispatch)
    ctx._start_worker_pool(pool_size=1)
    _serve_in_background(ctx, listen_socket)

    quitter = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    quitter.connect(str(path))
    quitter.sendall(b'{"jsonrpc":"2.0","id":"abandoned","method":"ping","params":{}}\n')
    time.sleep(0.1)
    quitter.close()  # the client is gone before the server can answer
    release.set()

    survivor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    survivor.settimeout(5)
    try:
        survivor.connect(str(path))
        survivor.sendall(b'{"jsonrpc":"2.0","id":"after","method":"ping","params":{}}\n')
        response = json.loads(survivor.makefile("rb").readline())
    finally:
        survivor.close()
        listen_socket.close()

    assert response["id"] == "after", "the only worker died on the abandoned connection"


@posix_only
def test_the_elected_socket_sits_in_a_0700_directory(short_tmp_path, monkeypatch) -> None:
    """The actual security boundary on POSIX. Not the socket's own mode --
    macOS/BSD do not reliably enforce those on connect."""
    import os
    import stat

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(short_tmp_path))
    path = short_tmp_path / "svc" / "tok.sock"

    old = os.umask(0o022)
    try:
        listen_socket = election.elect_unix_socket(path)
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(os.lstat(path.parent).st_mode) == 0o700
    finally:
        listen_socket.close()


@posix_only
def test_breadcrumb_liveness_reads_a_real_unix_endpoint(short_tmp_path, monkeypatch) -> None:
    """`should_spawn`'s wedged-server check has to work on POSIX too: pid
    liveness alone proves the process is running, not that it is serving."""
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(short_tmp_path))
    path = short_tmp_path / "svc" / "tok.sock"

    listen_socket = election.elect_unix_socket(path)
    try:
        assert breadcrumb._pipe_is_alive(str(path)) is True
    finally:
        listen_socket.close()

    # Closed without unlinking -- the hard-kill shape. The file is still
    # there and must NOT read as alive.
    assert breadcrumb._pipe_is_alive(str(path)) is False
