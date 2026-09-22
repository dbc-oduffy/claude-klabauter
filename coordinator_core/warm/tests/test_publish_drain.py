"""A publish must not kill the warm server's in-flight requests.

Pins the two defects behind state/bug-backlog/2026-08-29-the-publish-swap-
races-the-warm-servers-running-from-the-tree-it-replaces.yaml (macOS half):

  - after `close_listener` a POSIX acceptor dropped one late caller and
    exited, so every later caller sat unanswered in the kernel backlog until
    the drain ceiling ended the process;
  - a drain polled only live connections, so a pool task whose connection
    had already given up was killed mid-op at exit.

Not `cadence`-marked, unlike `test_server_posix.py`: these are regression
pins for the fast tier. Threads and a unix socket only; no process spawns.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, election, idle, lifecycle, server

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs a POSIX kernel: the server's AF_UNIX bind/accept path does not exist on Windows",
)


@pytest.fixture(autouse=True)
def _reset_shutdown_guard():
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    yield
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


@pytest.fixture()
def short_tmp_path():
    """The suite-root warm-runtime base (short enough for `sun_path`), used
    as a stamped engine root -- same shape as `test_server_posix.py`'s."""
    base = Path(os.environ[breadcrumb.RUNTIME_BASE_ENV])
    stamp = base / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("test-engine-stamp\n", encoding="utf-8")
    return base


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False


class _FakeListener:
    def __init__(self, results):
        self._results = list(results)
        self.accept_calls = 0

    def accept(self):
        self.accept_calls += 1
        if not self._results:
            raise OSError("listener closed")
        return self._results.pop(0), "peer"


class _FakeConn:
    def close(self):
        pass


def _ctx(**kwargs):
    kwargs.setdefault("name", "/run/u/coordinator/warm/hash/tok.sock")
    kwargs.setdefault("sid", "501")
    kwargs.setdefault("version_state", _FakeVersionState())
    return server._ServerContext(**kwargs)


def test_acceptor_refuses_and_keeps_accepting_after_close_listener(monkeypatch) -> None:
    """A draining acceptor answers every late connection itself and keeps
    going. The old shape closed one and returned, so past
    ACCEPTOR_POOL_SIZE late callers nothing accepted and the rest waited
    in the backlog until exit. Refusals are never enqueued or counted in
    flight, or traffic would extend the drain."""
    refused = []
    monkeypatch.setattr(
        server, "_refuse_while_draining", lambda conn, *, server_sha: refused.append(conn)
    )
    ctx = _ctx()
    ctx.close_listener()
    late = [_FakeConn(), _FakeConn(), _FakeConn()]
    listener = _FakeListener(late)

    ctx._acceptor_loop(listener)

    assert refused == late
    assert listener.accept_calls == len(late) + 1  # stops only when the socket closes
    assert ctx._queue.qsize() == 0
    assert ctx.in_flight() == 0


def test_a_pool_task_outliving_its_connection_still_holds_the_drain(monkeypatch) -> None:
    """The connection thread gives up at `_POOL_RESULT_DEADLINE_SECS` and
    releases its slot, but the worker process is still running the op.
    The drain must wait for the op to settle, not only for the connection:
    exiting kills the worker mid-op via its parent watchdog."""
    import concurrent.futures

    running = concurrent.futures.Future()
    assert running.set_running_or_notify_cancel()

    class _Pool:
        def submit(self, fn, *args):
            return running

    ctx = _ctx()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: _Pool())
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.01)

    response = ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})

    assert "error" in response  # the connection got its timeout envelope
    assert ctx.in_flight() == 0
    assert ctx.drain_outstanding() == 1
    running.set_result({"jsonrpc": "2.0", "id": 1, "result": "late"})
    assert ctx.drain_outstanding() == 0


def test_every_shutdown_trigger_drains_on_drain_outstanding(monkeypatch) -> None:
    """Both of this server's shutdown triggers -- skew eviction and idle
    demotion -- must poll `drain_outstanding`, not bare `in_flight`."""
    seen = {}
    monkeypatch.setattr(
        lifecycle, "drain_and_exit", lambda **kw: seen.setdefault("skew", kw["in_flight_count"])
    )
    monkeypatch.setattr(
        idle, "demote_if_idle", lambda **kw: seen.setdefault("idle", kw["in_flight_count"])
    )
    monkeypatch.setattr(server.push_cadence, "on_idle_tick", lambda **kw: None)
    monkeypatch.setattr(server.telemetry, "record_worker_pool_depth", lambda **kw: None)
    ctx = _ctx()

    ctx._drain()
    ctx._idle_tick()

    assert seen == {"skew": ctx.drain_outstanding, "idle": ctx.drain_outstanding}




@posix_only
def test_a_skew_eviction_does_not_kill_in_flight_or_late_requests(short_tmp_path, monkeypatch) -> None:
    """A publish, end to end on a real unix socket: one request is
    mid-dispatch when a post-publish request evicts the server on skew.
    The in-flight request must get its REAL answer before the server exits,
    and callers arriving during the drain -- more of them than there are
    acceptor threads -- must each get a prompt ENGINE_SKEW rather than
    waiting in the kernel backlog for the exit.

    `_drain` is rebound to the production `drain_and_exit` with only
    `exit_fn` faked, so the drain predicate and ordering are the real ones.
    """
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(short_tmp_path))
    path = short_tmp_path / "svc" / "tok.sock"
    listen_socket = election.elect_unix_socket(path)

    class _PublishedUnder(_FakeVersionState):
        def is_skewed(self, client_token: str) -> bool:
            return client_token == "post-publish"

    release = threading.Event()
    exited = threading.Event()
    answered_before_exit = []

    ctx = _ctx(
        name=str(path),
        engine_root=short_tmp_path,
        listen_socket=listen_socket,
        endpoint_path=path,
        version_state=_PublishedUnder(),
    )

    def _dispatch(self, msg, *, caller=None, isolated=False):
        release.wait(10)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "served"}

    def _exit(code):
        answered_before_exit.append(ctx.drain_outstanding() == 0)
        exited.set()

    monkeypatch.setattr(type(ctx), "_pool_dispatch", _dispatch)
    monkeypatch.setattr(
        ctx,
        "_drain",
        lambda: lifecycle.drain_and_exit(
            in_flight_count=ctx.drain_outstanding,
            ctx_shutdown=lambda: None,
            exit_fn=_exit,
            drain_ceiling_secs=10,
        ),
    )
    ctx._start_worker_pool(pool_size=2)
    threading.Thread(
        target=lambda: ctx._start_acceptor_pool(listen_socket, pool_size=1), daemon=True
    ).start()

    def _send(token, request_id):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(path))
        frame = {"jsonrpc": "2.0", "id": request_id, "method": "ping", "params": {}, "_engine_token": token}
        client.sendall((json.dumps(frame) + "\n").encode("utf-8"))
        return client

    def _read(client):
        try:
            return json.loads(client.makefile("rb").readline())
        finally:
            client.close()

    try:
        in_flight = _send("pre-publish", "in-flight")
        deadline = time.monotonic() + 5
        while ctx.in_flight() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        evicting = _read(_send("post-publish", "evicting"))
        late = [_read(_send("post-publish", f"late-{i}")) for i in range(3)]

        assert not exited.is_set(), "the server exited with a request still in flight"
        release.set()
        in_flight_response = _read(in_flight)
        assert exited.wait(5)
    finally:
        release.set()
        listen_socket.close()

    assert evicting["error"]["code"] == server.skew.ENGINE_SKEW
    assert [r["error"]["code"] for r in late] == [server.skew.ENGINE_SKEW] * 3
    assert [r["id"] for r in late] == ["late-0", "late-1", "late-2"]
    assert in_flight_response == {"jsonrpc": "2.0", "id": "in-flight", "result": "served"}
    assert answered_before_exit == [True]
