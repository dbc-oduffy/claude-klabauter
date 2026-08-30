"""Tests for coordinator_core.warm.server -- the C30 accept loop / dispatch
/ boot-identity entrypoint.

Exercises the connection layer (`_serve_line`, `_handle_connection`,
`InFlightCounter`) and the dispatch seam (`_run_dispatch`) directly, with
fake connection objects standing in for a real named pipe, rather than
opening actual Windows named pipes. This module's own accept loop
(`_ServerContext.serve_forever`) is thin, OS-pipe-specific glue around
these already-tested pieces (create instance, connect, wrap, hand off to a
thread) -- see `election.py` / `skew.py` / `lifecycle.py`'s own test
suites for the primitives it composes, and the 2026-08-14 transport-spike
verdict this module's docstring cites for the wrap-a-handle-as-a-blocking-
file-object shape, which was verified end-to-end on this box (throwaway
probe) before this file was written. Testing at this layer keeps the four
C20-anchored behaviors below deterministic and importable on non-Windows,
per this module's own "must import on non-Windows" constraint.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C30

Spawn ratchet C2 disposition: TIER --
`test_idle_demotion_survives_token_rotation_after_serving` runs the
predecessor in a real subprocess because `idle.py`'s clock is
process-global (module docstring), so two servers' idle state cannot be
faithfully modelled in one process; a clean interpreter is load-bearing for
that one test, and Rule 4 tiers at file granularity.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.session import declared_writes
from coordinator_core.warm import breadcrumb, election, idle, lifecycle, server, skew, telemetry

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _reset_shutdown_guard():
    """`warm.lifecycle`'s single-shot shutdown guard is process-global
    (module docstring: "must be entered at most once per server life").
    Reset around every test so one test's `drain_and_exit` call does not
    silently no-op the next test's."""
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    yield
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


class _FakeIO:
    """Stand-in for the blocking file object `server._wrap_handle` returns
    -- same `.readline()` / `.write()` / `.close()` surface, backed by an
    in-memory queue instead of a real pipe handle."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.written: list[bytes] = []
        self.closed = False

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeVersionState:
    def __init__(self, *, skewed: bool = False, server_sha: str = "deadbeef"):
        self._skewed = skewed
        self.server_sha = server_sha

    def is_skewed(self, client_token: str) -> bool:
        return self._skewed


def _frame(*, id_, method="noop", extra=None, token="client-token") -> bytes:
    """A frame from a TRUSTED caller by default.

    `_engine_token` is stamped here because a request that omits the
    field is refused with `UNTRUSTED_CALLER_ERROR` before `dispatch` is
    ever reached, so a tokenless frame cannot exercise anything
    downstream of the trust gate. Rejection itself is pinned by
    `test_untrusted_caller_token.py`, which asks for that shape explicitly
    with `token=None`.
    """
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": {}}
    if token is not None:
        msg["_engine_token"] = token
    if extra:
        msg.update(extra)
    return (json.dumps(msg) + "\n").encode("utf-8")


def _written_responses(io_obj: _FakeIO) -> list[dict]:
    return [json.loads(line) for line in io_obj.written]


def test_overlapping_dispatch_keeps_distinct_identities():
    """Two connections dispatched concurrently on their own threads must
    not observe each other's declared-writes state -- the multi-op
    isolation property the whole warm-engine premise rests on
    (test_warm_tier.py::test_overlapping_dispatch_keeps_distinct_identities).
    """
    seen: dict[int, list[str]] = {}
    barrier = threading.Barrier(2)

    def _dispatch(msg: dict, *, caller=None, isolated=False) -> dict:
        declared_writes.declare_write(f"path-{msg['id']}.txt")
        barrier.wait(timeout=5)  # force the two threads to overlap
        seen[msg["id"]] = list(declared_writes.active_declarations() or [])
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    def _run(io_obj: _FakeIO) -> None:
        # A real connection thread opens its own scope via `_run_dispatch`'s
        # `per_request_state()`; this test opens the same primitive directly
        # around the fake `_dispatch` above so isolation is observable
        # without a real op registry dispatch in the loop.
        with declared_writes.collecting():
            server._handle_connection(
                io_obj,
                version_state=_FakeVersionState(),
                server_sha="x",
                close_listener=lambda: None,
                drain=lambda: None,
                in_flight=server.InFlightCounter(),
                dispatch=_dispatch,
            )

    ios = [_FakeIO([_frame(id_=1)]), _FakeIO([_frame(id_=2)])]
    threads = [threading.Thread(target=_run, args=(io_obj,)) for io_obj in ios]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert seen[1] == ["path-1.txt"]
    assert seen[2] == ["path-2.txt"]
    assert _written_responses(ios[0])[0]["result"] == "ok"
    assert _written_responses(ios[1])[0]["result"] == "ok"


def test_wedged_op_does_not_stall_the_next():
    """A hung dispatch on one connection's thread must not block a fresh
    dispatch on another (test_warm_tier.py::test_wedged_op_does_not_stall_the_next)."""
    wedge_released = threading.Event()
    order: list[str] = []
    order_lock = threading.Lock()

    def _dispatch(msg: dict, *, caller=None, isolated=False) -> dict:
        if msg["id"] == "wedged":
            wedge_released.wait(timeout=5)
        with order_lock:
            order.append(msg["id"])
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    in_flight = server.InFlightCounter()
    version_state = _FakeVersionState()
    io_wedged = _FakeIO([_frame(id_="wedged")])
    io_fast = _FakeIO([_frame(id_="fast")])

    kwargs = dict(
        version_state=version_state,
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=in_flight,
        dispatch=_dispatch,
    )
    t_wedged = threading.Thread(target=server._handle_connection, args=(io_wedged,), kwargs=kwargs)
    t_wedged.start()

    t_fast = threading.Thread(target=server._handle_connection, args=(io_fast,), kwargs=kwargs)
    t_fast.start()
    t_fast.join(timeout=5)

    assert not t_fast.is_alive()
    assert order == ["fast"]
    assert _written_responses(io_fast)[0]["result"] == "ok"
    assert t_wedged.is_alive()  # still wedged -- proves it never blocked the fast one

    wedge_released.set()
    t_wedged.join(timeout=5)
    assert order == ["fast", "wedged"]
    assert _written_responses(io_wedged)[0]["result"] == "ok"


def test_shutdown_trigger_mid_request_completes_in_flight_before_exit():
    """A skew-detected shutdown mid-request must write the request's own
    response BEFORE the drain sequence runs `exit_fn`, and the request's
    own in-flight slot must already be released by the time the drain wait
    is polled -- else `drain_and_exit`'s `_wait_for_drain` would deadlock
    waiting on its own caller's count
    (test_warm_tier.py::test_skew_eviction_under_concurrent_clients, and
    the shutdown-mid-request behavior C20 names directly)."""
    exit_calls: list[int] = []
    listener_closed: list[bool] = []
    in_flight = server.InFlightCounter()
    version_state = _FakeVersionState(skewed=True, server_sha="abc123")
    io_obj = _FakeIO([_frame(id_=7, extra={"_engine_token": "client-token"})])

    def drain() -> None:
        # In-flight must already be zero here -- this request's own slot
        # was released by `_serve_line` before `evict_on_skew` ever calls
        # `drain`, so this assertion is the deadlock guard, not a formality.
        assert in_flight() == 0
        lifecycle.drain_and_exit(
            in_flight_count=in_flight,
            ctx_shutdown=lambda: None,
            exit_fn=exit_calls.append,
        )

    server._handle_connection(
        io_obj,
        version_state=version_state,
        server_sha=version_state.server_sha,
        close_listener=lambda: listener_closed.append(True),
        drain=drain,
        in_flight=in_flight,
        dispatch=lambda msg: pytest.fail("dispatch must not run on a skewed request"),
    )

    assert listener_closed == [True]
    assert exit_calls == [0]
    assert in_flight() == 0

    response = _written_responses(io_obj)[0]
    assert response["error"]["code"] == skew.ENGINE_SKEW
    assert response["error"]["data"]["server_sha"] == "abc123"


def test_shutdown_trigger_waits_for_a_separate_in_flight_request_before_exit():
    """The drain wait must genuinely block on an OTHER request that is
    still in flight on a different connection when a shutdown trigger
    fires -- not merely observe its own (already-released) slot and
    declare the sequence's return value proof enough. This is the real
    counter `InFlightCounter` owns (there is no live in-flight counter
    anywhere else in `warm/` -- `coordinator_core.lifecycle`'s
    `get_in_flight_count`/`in_flight_increment`/`in_flight_decrement` are
    C3-retired no-ops; `warm.lifecycle.drain_and_exit`'s `in_flight_count`
    is a caller-injected callable for exactly this reason)."""
    in_flight = server.InFlightCounter()
    hold = threading.Event()
    other_done = threading.Event()
    exit_calls: list[int] = []

    def dispatch_a(msg: dict, *, caller=None, isolated=False) -> dict:
        hold.wait(timeout=5)
        other_done.set()
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_a = _FakeIO([_frame(id_="a")])
    t_a = threading.Thread(
        target=server._handle_connection,
        args=(io_a,),
        kwargs=dict(
            version_state=_FakeVersionState(),
            server_sha="x",
            close_listener=lambda: None,
            drain=lambda: None,
            in_flight=in_flight,
            dispatch=dispatch_a,
        ),
    )
    t_a.start()

    deadline = time.monotonic() + 5
    while in_flight() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert in_flight() == 1  # A is genuinely counted in-flight before B triggers shutdown

    def drain() -> None:
        lifecycle.drain_and_exit(
            in_flight_count=in_flight,
            ctx_shutdown=lambda: None,
            exit_fn=exit_calls.append,
        )

    io_b = _FakeIO([_frame(id_="b", extra={"_engine_token": "tok"})])
    t_b = threading.Thread(
        target=server._handle_connection,
        args=(io_b,),
        kwargs=dict(
            version_state=_FakeVersionState(skewed=True, server_sha="abc"),
            server_sha="abc",
            close_listener=lambda: None,
            drain=drain,
            in_flight=in_flight,
            dispatch=lambda msg: pytest.fail("dispatch must not run on a skewed request"),
        ),
    )
    t_b.start()

    # The drain loop has had time to start polling; it must NOT have
    # reached exit yet, because A is still genuinely in flight.
    time.sleep(0.2)
    assert exit_calls == []
    assert not other_done.is_set()

    hold.set()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    assert other_done.is_set()
    assert exit_calls == [0]
    assert in_flight() == 0
    assert json.loads(io_a.written[0])["result"] == "ok"


def test_malformed_frame_does_not_kill_the_loop():
    """A frame that is not valid UTF-8/JSON/an-object must produce a
    well-formed error response on its own connection, never an unhandled
    exception (test_warm_tier.py's "cold fallback under every warm
    failure" -- the server-side half of that contract)."""
    cases = [b"not json at all\n", b'"a json string, not an object"\n', b"\xff\xfe not utf-8\n"]
    for raw in cases:
        in_flight = server.InFlightCounter()
        io_obj = _FakeIO([raw])
        server._handle_connection(
            io_obj,
            version_state=_FakeVersionState(),
            server_sha="x",
            close_listener=lambda: None,
            drain=lambda: None,
            in_flight=in_flight,
            dispatch=lambda msg: pytest.fail("dispatch must not run for a malformed frame"),
        )
        response = _written_responses(io_obj)[0]
        assert response["error"]["code"] in (server.PARSE_ERROR, server.INVALID_REQUEST)
        assert in_flight() == 0
        assert io_obj.closed


def test_ordinary_dispatch_exception_still_surfaces_as_internal_error():
    """An op-level bug (any exception `dispatch` raises other than the
    `BrokenProcessPool` recovery `_pool_dispatch` already handles) must keep
    reaching the caller as an ordinary INTERNAL_ERROR envelope, unchanged --
    the negative-spec for the pool-death indeterminate branch: narrowing the
    dead-pool signal to `_pool_dispatch`'s own except clause must not widen
    what counts as 'engine unhealthy' at this generic seam."""
    in_flight = server.InFlightCounter()
    io_obj = _FakeIO([_frame(id_=1)])

    def _raising_dispatch(msg, *, caller=None, isolated=False):
        raise ValueError("some ordinary op bug")

    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=in_flight,
        dispatch=_raising_dispatch,
    )
    response = _written_responses(io_obj)[0]
    assert response["error"]["code"] == server.INTERNAL_ERROR
    assert "some ordinary op bug" in response["error"]["message"]


def test_run_dispatch_opens_per_request_state(monkeypatch):
    """`_run_dispatch` must open `entry_seam.per_request_state()` around
    every call into `coordinator_core.ipc.dispatch_message` -- a fresh,
    empty declared-writes collection per call, closed again on return."""
    captured: dict[str, object] = {}

    async def fake_dispatch_message(msg: dict, *, caller: str | None = None) -> dict:
        captured["active_during"] = list(declared_writes.active_declarations() or [])
        declared_writes.declare_write("inner.txt")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": "ok"}

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", fake_dispatch_message)

    assert declared_writes.active_declarations() is None
    result = server._run_dispatch({"jsonrpc": "2.0", "id": 1, "method": "noop", "params": {}})

    assert result["result"] == "ok"
    assert captured["active_during"] == []
    assert declared_writes.active_declarations() is None


def test_run_dispatch_captures_handler_stdout_into_stderr_field(monkeypatch):
    """W9: a handler `print()` served warm must not be silently discarded --
    `_run_dispatch` must capture it and fold it into the `_stderr` sibling
    field `warm.client` already pops and relays, the same field the
    pre-existing `diagnostics` mechanism uses."""

    async def fake_dispatch_message(msg: dict, *, caller: str | None = None) -> dict:
        print("handler stdout line")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": "ok"}

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", fake_dispatch_message)

    result = server._run_dispatch({"jsonrpc": "2.0", "id": 1, "method": "noop", "params": {}})

    assert result["result"] == "ok"
    assert "handler stdout line" in result["_stderr"]


def test_pool_dispatch_worker_captures_handler_stdout_into_stderr_field(monkeypatch):
    """Same contract as `_run_dispatch`, for the process-pool worker target
    -- the worker binds stdout to `os.devnull`
    (`_bind_null_std_streams`/`_suppress_pool_worker_consoles`), so this is
    the ONLY point that can relay a handler's `print()` warm-served through
    the pool."""

    async def fake_dispatch_message(msg: dict, *, caller: str | None = None) -> dict:
        print("pool worker stdout line")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": "ok"}

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", fake_dispatch_message)

    result = server._pool_dispatch_worker({"jsonrpc": "2.0", "id": 1, "method": "noop", "params": {}}, None)

    assert result["result"] == "ok"
    assert "pool worker stdout line" in result["_stderr"]


# ---------------------------------------------------------------------------
# C6 -- a warm-served refusal keeps its diagnostic. `emit_diagnostic`'s 3
# call sites are bridged by `entry_seam`'s own sink already (see
# test_setup_error_reaches_a_warm_caller.py); the other ~1512 sites write
# straight to `sys.stderr`, which only a REAL stderr capture -- not the
# sink -- can bridge. These pin that bridge, and tie it to
# `cc_invoke.RouteMutationError.op_stderr`, the field root cause 1 names.
# ---------------------------------------------------------------------------

def test_run_dispatch_captures_raw_handler_stderr_write_into_stderr_field(monkeypatch):
    """A REFUSING op's own diagnostic sentence, written directly to
    `sys.stderr` (not via `emit_diagnostic`) -- the shape of the other 1512
    call sites -- must still arrive in the `_stderr` sibling field warm
    dispatch already relays."""

    async def fake_dispatch_message(msg: dict, *, caller: str | None = None) -> dict:
        import sys as _sys

        _sys.stderr.write("refusing op: scoped_to.sha does not resolve\n")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"exit_code": 1}}

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", fake_dispatch_message)

    result = server._run_dispatch({"jsonrpc": "2.0", "id": 1, "method": "memo.send", "params": {}})

    assert result["result"] == {"exit_code": 1}
    assert "scoped_to.sha does not resolve" in result["_stderr"]


def test_pool_dispatch_worker_captures_raw_handler_stderr_write_into_stderr_field(monkeypatch):
    """Same contract as `_run_dispatch`, for the process-pool worker target."""

    async def fake_dispatch_message(msg: dict, *, caller: str | None = None) -> dict:
        import sys as _sys

        _sys.stderr.write("pool worker refusal: receiver unresolvable\n")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"exit_code": 1}}

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", fake_dispatch_message)

    result = server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 1, "method": "memo.send", "params": {}}, None
    )

    assert result["result"] == {"exit_code": 1}
    assert "receiver unresolvable" in result["_stderr"]


def test_captured_stderr_sentence_reaches_route_mutation_error_op_stderr(monkeypatch):
    """Ties the bridge to root cause 1: `cc_invoke.route_mutation` builds
    `RouteMutationError.op_stderr` from the child's captured stderr text, and
    a warm-served refusal's `_stderr` frame field is exactly that text once
    `warm.client` relays it onto this process's own stderr (see
    `test_client_pops_and_reemits_rather_than_passing_the_key_through` for
    the relay pin). This drives the SAME sentence through `_run_dispatch`
    and then through `RouteMutationError` construction the way
    `route_mutation` does, proving the field -- not merely "a reader" --
    carries it.
    """
    import sys as _cc_invoke_path_setup

    lib_dir = str(
        (Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "lib")
    )
    if lib_dir not in _cc_invoke_path_setup.path:
        _cc_invoke_path_setup.path.insert(0, lib_dir)
    import cc_invoke

    async def fake_dispatch_message(msg: dict, *, caller: str | None = None) -> dict:
        import sys as _sys

        _sys.stderr.write("fleet op setup error: the receiver is not registered\n")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"exit_code": 1, "mode": "send"}}

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", fake_dispatch_message)

    response = server._run_dispatch({"jsonrpc": "2.0", "id": 1, "method": "memo.send", "params": {}})
    relayed_stderr = response["_stderr"]
    assert "the receiver is not registered" in relayed_stderr

    message = cc_invoke.mutation_refusal_message(
        "memo.send", response["result"], op_stderr=relayed_stderr
    )
    exc = cc_invoke.RouteMutationError(message, response["result"], op_stderr=relayed_stderr)

    assert "the receiver is not registered" in exc.op_stderr


# ---------------------------------------------------------------------------
# Pending-listener pool (problem 3 of docs/problems/2026-08-19-the-warm-
# engine-serves-one-caller-at-a-t.md): `_accept_and_replenish` posts one
# replacement per accept, so steady state was exactly ONE pending listener
# regardless of demand. `_start_pending_listener_pool` posts N at boot
# instead of one, letting that same one-for-one replenish invariant sustain
# a constant pool of N rather than a pool of one.
# ---------------------------------------------------------------------------


def test_boot_pool_starts_one_accept_chain_per_pending_instance(monkeypatch):
    """`_start_pending_listener_pool` must start exactly `pool_size` accept
    chains -- one bound to the caller-supplied `first_handle` (election's
    own instance) and `pool_size - 1` bound to freshly created instances --
    each on its own thread, never serialized behind one another."""
    created_handles: list[int] = []

    def _fake_create(name, sid):
        created_handles.append(len(created_handles) + 100)
        return created_handles[-1]

    monkeypatch.setattr(server, "_create_pipe_instance", _fake_create)

    started_with: list[int] = []
    started_lock = threading.Lock()
    release = threading.Event()

    def _fake_accept_and_replenish(self, handle):
        with started_lock:
            started_with.append(handle)
        release.wait(timeout=5)  # hold the thread open, like a real blocked ConnectNamedPipe

    monkeypatch.setattr(server._ServerContext, "_accept_and_replenish", _fake_accept_and_replenish)

    ctx = server._ServerContext(name="pipe-x", sid="sid-x", version_state=_FakeVersionState())
    ctx._start_pending_listener_pool(1, pool_size=5)

    deadline = time.monotonic() + 5
    while len(started_with) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()

    assert sorted(started_with) == sorted([1] + created_handles)
    assert len(created_handles) == 4  # pool_size - 1 freshly created instances
    assert len(set(started_with)) == 5  # five DISTINCT chains, not one replayed


def test_boot_pool_creation_failure_shrinks_the_pool_without_raising(monkeypatch):
    """A `_create_pipe_instance` failure partway through boot must not
    raise past `_start_pending_listener_pool` -- a smaller-than-intended
    pool degrades toward today's one-listener behaviour rather than
    killing a server that can otherwise serve (module docstring's own
    best-effort contract for this step)."""
    calls = {"n": 0}

    def _fake_create(name, sid):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated CreateNamedPipe failure")
        return calls["n"] + 100

    monkeypatch.setattr(server, "_create_pipe_instance", _fake_create)

    started_with: list[int] = []
    release = threading.Event()

    def _fake_accept_and_replenish(self, handle):
        started_with.append(handle)
        release.wait(timeout=5)

    monkeypatch.setattr(server._ServerContext, "_accept_and_replenish", _fake_accept_and_replenish)

    ctx = server._ServerContext(name="pipe-x", sid="sid-x", version_state=_FakeVersionState())
    ctx._start_pending_listener_pool(1, pool_size=4)  # 3 creation attempts, 1 fails

    deadline = time.monotonic() + 5
    while len(started_with) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()

    assert len(started_with) == 3  # first_handle + 2 of the 3 creation attempts (one failed)


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows-only")
def test_pool_serves_more_than_one_simultaneous_connection(monkeypatch, tmp_path):
    """The refutation criterion problem 3 names directly: showing MORE THAN
    ONE instance outstanding in steady state. Runs a real `_ServerContext`
    against real Windows named pipes (own pipe name, isolated from any live
    server on this box) and proves two clients can hold an open connection
    to it AT THE SAME TIME -- impossible with the pre-fix single pending
    listener, where the second opener would see `FileNotFoundError` while
    the first connection is being handled.
    """
    import uuid
    from coordinator_core.warm import election

    monkeypatch.setattr(lifecycle, "reset_shutdown_guard_for_test", lifecycle.reset_shutdown_guard_for_test)
    sid = election.current_user_sid()
    name = election.pipe_name(uuid.uuid4().hex, user_sid=sid)
    first_handle = election.elect(name, user_sid=sid)

    ctx = server._ServerContext(name=name, sid=sid, version_state=_FakeVersionState())
    # Small pool -- just enough to prove ">1 outstanding", not a full 30.
    ctx._start_pending_listener_pool(first_handle, pool_size=3)

    # Give the accept threads a moment to post their ConnectNamedPipe calls.
    time.sleep(0.2)

    opened = []
    try:
        for _ in range(2):
            opened.append(open(name, "r+b"))
    finally:
        ctx.close_listener()
        for fh in opened:
            fh.close()

    assert len(opened) == 2  # both opens succeeded -- more than one instance was listening


# ---------------------------------------------------------------------------
# Idle-demotion / breadcrumb wiring (C30 gap: idle.py and breadcrumb.py had
# no production call site -- see the module docstring's "idle demotion" /
# "breadcrumb" ownership notes).
# ---------------------------------------------------------------------------


def test_idle_watchdog_demotes_a_zero_invocation_server(monkeypatch):
    """The idle watchdog thread must fire `idle.demote_if_idle` -- with a
    served-count of zero -- on its own, with NO connection ever having
    arrived on the accept loop. This is the leak the EM measured live: a
    stranded generation that never gets a request must still self-demote,
    which only an independent timer (not a request-path check) can do.

    `idle.demote_if_idle` itself is monkeypatched here (not exercised for
    real) purely to avoid this test process calling `os._exit` -- the
    thing under test is that `_ServerContext`'s watchdog reaches it at
    all, with the right `served_count`/`close_listener`/`ctx_shutdown`
    wiring, not `idle.py`'s own predicate (already covered by
    test_idle_demotion.py).
    """
    calls: list[int] = []

    def _fake_demote_if_idle(*, served_count, close_listener, in_flight_count, ctx_shutdown, **_kw):
        calls.append(served_count())
        close_listener()
        return True

    monkeypatch.setattr(server.idle, "demote_if_idle", _fake_demote_if_idle)
    monkeypatch.setattr(server, "_IDLE_WATCHDOG_POLL_SECS", 0.02)

    ctx = server._ServerContext(name="pipe-x", sid="sid-x", version_state=_FakeVersionState())
    t = threading.Thread(target=ctx._idle_watchdog_loop, daemon=True)
    t.start()

    deadline = time.monotonic() + 3
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    ctx._idle_watchdog_stop.set()
    t.join(timeout=3)

    assert calls == [0]  # zero-invocation server: served_count() reads 0
    assert ctx._is_listening() is False


def test_breadcrumb_exists_while_serving_and_gone_after_shutdown(tmp_path):
    """`_ctx_shutdown` must unlink the breadcrumb for THIS server's own
    `engine_root` -- the operator stop hatch (`warm-engine-stop.py`)
    otherwise reports "nothing to stop" for a server that is, in fact,
    still running (or, symmetrically, never learns a server has already
    exited).

    The breadcrumb is written under THIS process's pid because the unlink is
    ownership-checked (`breadcrumb.unlink_breadcrumb`'s `owner_pid`): a
    departing server clears only a breadcrumb that still names it, so that a
    superseded generation's exit cannot delete its live successor's entry. A
    synthetic pid here would exercise the refusal path, not the one this test
    is about -- `test_ctx_shutdown_does_not_clear_a_successors_breadcrumb`
    below covers the other side.
    """
    breadcrumb.write_breadcrumb(
        pipe="pipe-x",
        pid=os.getpid(),
        stable_pid_start_epoch=1,
        engine_sha="deadbeef",
        engine_root=tmp_path,
    )
    assert breadcrumb.read_breadcrumb(tmp_path) is not None

    ctx = server._ServerContext(
        name="pipe-x", sid="sid-x", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    ctx._ctx_shutdown()

    assert breadcrumb.read_breadcrumb(tmp_path) is None


def test_ctx_shutdown_does_not_clear_a_successors_breadcrumb(tmp_path):
    """The other side of the same contract, at the wiring level rather than
    the primitive's: a SUPERSEDED generation running `_ctx_shutdown` must
    leave the live successor's breadcrumb alone. Before the ownership check
    this deleted it, and `warm.idle`'s superseded arm made that fire within
    one watchdog poll of the successor binding."""
    breadcrumb.write_breadcrumb(
        pipe="pipe-T2",
        pid=os.getpid() + 1,  # the SUCCESSOR owns the clone's one breadcrumb
        stable_pid_start_epoch=2,
        engine_sha="successor",
        engine_root=tmp_path,
    )

    ctx = server._ServerContext(
        name="pipe-T1", sid="sid-x", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    ctx._ctx_shutdown()

    surviving = breadcrumb.read_breadcrumb(tmp_path)
    assert surviving is not None, "a superseded generation deleted the successor's breadcrumb"
    assert surviving["engine_sha"] == "successor"


def test_idle_demotion_survives_token_rotation_after_serving(tmp_path):
    """Composed regression for the leak's actual field shape (EM's
    addendum, example-retrieval-repo-f7's finding): rotation MINTS an orphan (a new
    engine-stamp fingerprint -> a new `compute_client_token` -> a new pipe
    name, binding a successor without contesting the predecessor's
    still-listening pipe -- `election.py`'s own docstring), and dead
    demotion is what makes that orphan immortal. A predecessor that DID
    serve a request before being stranded (so its idle clock was marked,
    unlike a never-served process) must still self-terminate on
    time-since-last-invocation -- not on the "never served anything"
    zero-served short-circuit, which a demotion fix could satisfy while
    leaving this exact real-world orphan alive.

    Rotation is a publish rewriting the engine stamp
    (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C4 --
    `compute_client_token` no longer reads `.git/HEAD`/ref state at all, so
    the git-ref rewrite this test used to rotate on is no longer a live
    signal; `skew.write_engine_stamp` is what a publish round actually
    calls, per `test_token_is_stale_flips_when_a_publish_rewrites_the_engine_stamp`
    above).

    Runs the predecessor in a REAL subprocess (idle.py's clock is
    process-global -- module docstring -- so two servers' idle state
    cannot be faithfully modelled in one process) with a short
    `engine.warm.idle_minutes` via the `MACHINE_LOCAL_*` registry env
    override, marks one invocation, then asserts the process exits on its
    own within the deadline with NO further activity.
    """
    skew.write_engine_stamp(tmp_path, "sha-before-rotation")
    token_before = skew.compute_client_token(tmp_path)
    skew.write_engine_stamp(tmp_path, "sha-after-rotation")
    token_after = skew.compute_client_token(tmp_path)
    assert token_before != token_after  # rotation mints a new generation token

    worker = tmp_path / "_predecessor_worker.py"
    worker.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from coordinator_core.warm import idle, lifecycle\n"
        "idle.mark_invocation()  # served one request before being stranded\n"
        "deadline = time.monotonic() + 30\n"
        "while time.monotonic() < deadline:\n"
        "    if idle.demote_if_idle(\n"
        "        served_count=lambda: 1,\n"
        "        close_listener=lambda: None,\n"
        "        in_flight_count=lambda: 0,\n"
        "        ctx_shutdown=lambda: None,\n"
        "    ):\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "else:\n"
        "    sys.exit(1)  # never demoted within the deadline\n"
        % str(_engine_clone_root_for_test()),
        encoding="utf-8",
    )

    import os as _os
    import subprocess

    env = dict(_os.environ)
    env["MACHINE_LOCAL_ENGINE_WARM_IDLE_MINUTES"] = "0.02"  # 1.2s

    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(worker)],
        env=env,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    elapsed = time.monotonic() - started

    assert proc.returncode == 0  # demoted on its own, not the sys.exit(1) escape hatch
    assert elapsed >= 1.0  # did not fire instantly -- genuinely waited out the idle deadline


def _engine_clone_root_for_test():
    from pathlib import Path

    return Path(server.__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# C-warm-identity -- per-request identity binding (state/bug-backlog/
# 2026-08-18-a-warm-server-stamps-every-op-it-serves-eeb801fc6bee.yaml)
# ---------------------------------------------------------------------------


def test_serve_line_resolves_the_caller_identity_carried_on_the_request(monkeypatch):
    """The decisive case: a server whose OWN environment carries session id
    A, serving a request from a client whose environment carried session id
    B (relayed as the request's `_session_id` field), must resolve B for
    that dispatch -- not A, its own spawning session's id -- and must be
    back to resolving A once the dispatch returns, proving the Token/reset
    unwind (`session.core.session_identity_override`) actually fires rather
    than leaking across requests.
    """
    from coordinator_core.session import core as session_core

    session_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    session_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_a)
    assert session_core.resolve_session_id() == session_a  # server's own default, pre-dispatch

    from coordinator_core.warm.entry_seam import per_request_state

    observed: list[str] = []

    def _dispatch(msg: dict, *, caller=None, isolated=False) -> dict:
        # Mirrors `server._run_dispatch`'s own real binding of the seam --
        # this stub swaps out only the `asyncio.run(dispatch_message(...))`
        # body, not the per-request scoping around it.
        with per_request_state(session_id=caller.session_id if caller else None, isolated=True):
            observed.append(session_core.resolve_session_id())
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_obj = _FakeIO([_frame(id_="req-1", extra={"_caller": {"session_id": session_b}})])
    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        dispatch=_dispatch,
    )

    assert observed == [session_b]  # resolved the CALLER's id, not the server's own
    assert session_core.resolve_session_id() == session_a  # unwound back to the server's own default
    # `_caller` never reaches the dispatched handler's own msg -- popped
    # the same way `_engine_token` already is.
    assert "_caller" not in json.loads(io_obj.written[0])  # response frame, not the request


def test_run_dispatch_itself_binds_the_given_session_id(monkeypatch):
    """Exercises the REAL `server._run_dispatch` (the production dispatch
    seam every connection actually calls, not a hand-rolled stand-in) --
    only its leaf `ipc.dispatch_message` call is replaced, so the
    `per_request_state(session_id=...)` bind this function itself performs
    is real production code, not test-side reimplementation.

    Takes `caller` (a `CallerContext`), not a bare `session_id`: C1b widened
    the dispatch chain to the caller's identity SET."""
    from coordinator_core import ipc
    from coordinator_core.session import core as session_core

    session_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    async def _fake_dispatch_message(msg, *, caller=None):
        return {"jsonrpc": "2.0", "id": msg["id"], "result": session_core.resolve_session_id()}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    from coordinator_core.warm.caller_context import resolve_caller_context

    response = server._run_dispatch(
        {"jsonrpc": "2.0", "id": "1", "method": "noop", "params": {}},
        caller=resolve_caller_context({"session_id": session_b}),
        isolated=True,
    )
    assert response["result"] == session_b
    assert session_core.resolve_session_id() != session_b  # unwound after the call


def test_serve_line_with_no_carried_identity_strips_rather_than_falls_back(monkeypatch):
    """No-identity, isolated (C3 supersedes this test's pre-C3 fall-back
    expectation): a request that carries no `_caller` at all (older
    client, or a caller `resolve_session_id()` could not identify) must
    resolve NOTHING -- never the server's own spawner-named environment,
    which is the exact misattribution `session.core.carried_session_id()`'s
    omit-never-substitute contract forbids. `os.environ` is restored to the
    server's own value once the block closes, proving the strip is scoped to
    the dispatch and not a permanent mutation."""
    from coordinator_core.session import core as session_core

    session_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_a)

    from coordinator_core.warm.entry_seam import per_request_state

    observed: list[str] = []

    def _dispatch(msg: dict, *, caller=None, isolated=False) -> dict:
        with per_request_state(session_id=caller.session_id if caller else None, isolated=True):
            observed.append(session_core.resolve_session_id())
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_obj = _FakeIO([_frame(id_="req-1")])  # no _caller field at all
    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        dispatch=_dispatch,
    )

    assert observed == [""]
    assert session_core.resolve_session_id() == session_a


# ---------------------------------------------------------------------------
# Accept-and-queue (docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
# § C5, AC7/AC8): `_enqueue_connection` claims the in-flight slot at ENQUEUE,
# then hands `io` to one `queue.Queue` a fixed `WORKER_POOL_SIZE` pool of
# worker threads (`_worker_loop`) drains -- dispatch concurrency is bounded
# independently of how fast connections are accepted, and a drain must wait
# for accepted-but-not-yet-dispatched work, never merely for the workers
# that happen to be busy right now.
# ---------------------------------------------------------------------------


def test_enqueue_connection_counts_in_flight_before_a_worker_ever_runs():
    """`_enqueue_connection`'s own claim (module docstring's DRAIN
    SEMANTICS): the in-flight slot is taken at enqueue time, before any
    worker thread has even been started -- a request sitting in the queue
    with nothing consuming it yet must still count as in-flight."""
    ctx = server._ServerContext(name="pipe-enqueue", sid="sid-enqueue", version_state=_FakeVersionState())
    assert ctx.in_flight() == 0
    ctx._enqueue_connection("io-obj")
    assert ctx.in_flight() == 1
    assert ctx._queue.get_nowait() == "io-obj"


def test_worker_pool_bounds_concurrent_dispatch(monkeypatch):
    """The refutation criterion AC7 names directly: dispatch concurrency
    must be capped at `WORKER_POOL_SIZE` (here a small test pool) rather
    than growing one handler thread per accepted connection -- five
    connections enqueued against a two-worker pool must never show more
    than two running at once."""
    running: list[str] = []
    max_running = [0]
    lock = threading.Lock()
    release = threading.Event()

    def _fake_handle_connection(io, **kwargs):
        with lock:
            running.append(io)
            max_running[0] = max(max_running[0], len(running))
        release.wait(timeout=5)
        with lock:
            running.remove(io)

    monkeypatch.setattr(server, "_handle_connection", _fake_handle_connection)

    ctx = server._ServerContext(name="pipe-bound", sid="sid-bound", version_state=_FakeVersionState())
    ctx._start_worker_pool(pool_size=2)

    for i in range(5):
        ctx._enqueue_connection(f"io-{i}")

    deadline = time.monotonic() + 5
    while len(running) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(running) == 2  # bounded at the pool size, not the 5 enqueued

    release.set()  # let every held and future call through without blocking

    deadline = time.monotonic() + 5
    while not ctx._queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert max_running[0] == 2  # never exceeded the worker pool size


def test_worker_loop_survives_an_unhandled_exception_from_handle_connection(monkeypatch):
    """A worker thread must outlive a single connection's failure -- the
    defect this closes: `_write_and_release`'s `io.write(...)` (a common
    case under load, per `warm.client`'s own comment: a caller that hit its
    own `READ_DEADLINE_SECS` already closed its pipe) raises an unhandled
    `OSError` that neither `_serve_line` nor `_handle_connection` catches.
    Before the fix, that exception escaped `_worker_loop`'s `while True:`
    and killed the thread permanently -- live evidence found only 6 of
    `WORKER_POOL_SIZE`'s 30 numbered worker threads still alive on the
    resident server. This asserts the thread survives one failing
    connection and goes on to serve the next queued item."""
    calls: list[str] = []

    def _fake_handle_connection(io, **kwargs):
        calls.append(io)
        if io == "boom":
            raise OSError("simulated write to an abandoned connection")

    monkeypatch.setattr(server, "_handle_connection", _fake_handle_connection)

    ctx = server._ServerContext(name="pipe-survive", sid="sid-survive", version_state=_FakeVersionState())
    ctx._start_worker_pool(pool_size=1)

    ctx._enqueue_connection("boom")
    ctx._enqueue_connection("after")

    deadline = time.monotonic() + 5
    while calls != ["boom", "after"] and time.monotonic() < deadline:
        time.sleep(0.01)

    assert calls == ["boom", "after"]  # the worker kept draining after "boom" raised


def test_drain_waits_for_queued_but_not_yet_dispatched_work():
    """AC8's own refutation criterion: a request queued behind a busy
    worker must not be dropped at shutdown -- `drain_and_exit` must block
    until the queued item is actually dispatched and released, not merely
    until the currently-busy worker's own request finishes."""
    started_first = threading.Event()
    hold_first = threading.Event()
    dispatched_second = threading.Event()

    def _fake_handle_connection(io, *, in_flight, **kwargs):
        if io == "first":
            started_first.set()
            hold_first.wait(timeout=5)
        else:
            dispatched_second.set()
        in_flight.exit()

    ctx = server._ServerContext(name="pipe-drain", sid="sid-drain", version_state=_FakeVersionState())
    orig_handle_connection = server._handle_connection
    server._handle_connection = _fake_handle_connection
    try:
        ctx._start_worker_pool(pool_size=1)

        ctx._enqueue_connection("first")
        assert started_first.wait(timeout=5)
        ctx._enqueue_connection("second")

        # Both are already counted in-flight -- "second" is queued, unstarted.
        assert ctx.in_flight() == 2

        exit_calls: list[int] = []
        drain_thread = threading.Thread(
            target=lambda: lifecycle.drain_and_exit(
                in_flight_count=ctx.in_flight,
                ctx_shutdown=lambda: None,
                exit_fn=exit_calls.append,
            )
        )
        drain_thread.start()

        time.sleep(0.2)
        assert exit_calls == []  # "second" is still queued -- must not have drained yet
        assert not dispatched_second.is_set()

        hold_first.set()
        drain_thread.join(timeout=5)

        assert dispatched_second.is_set()
        assert exit_calls == [0]
        assert ctx.in_flight() == 0
    finally:
        server._handle_connection = orig_handle_connection


# ---------------------------------------------------------------------------
# Superseded-generation retirement (the publish-strands-an-engine defect)
# ---------------------------------------------------------------------------


def test_token_is_stale_flips_when_a_publish_rewrites_the_engine_stamp(tmp_path):
    """The defect's actual trigger, reproduced through the real publish
    seam rather than a synthetic token: `skew.write_engine_stamp` is what
    a publish round calls, and rewriting it is what rotates the generation
    token out from under a resident server."""
    skew.write_engine_stamp(tmp_path, "sha-before-publish")
    boot_token = skew.compute_client_token(tmp_path)

    ctx = server._ServerContext(
        name=election.pipe_name(boot_token, engine_clone=tmp_path, user_sid="S-1-5-21-TEST"),
        sid="S-1-5-21-TEST",
        version_state=_FakeVersionState(),
        engine_root=tmp_path,
        boot_token=boot_token,
    )
    assert ctx._token_is_stale() is False

    skew.write_engine_stamp(tmp_path, "sha-after-publish")

    assert ctx._token_is_stale() is True
    # ...and the reason it is unreachable: no client computes its pipe name.
    assert (
        election.pipe_name(
            skew.compute_client_token(tmp_path),
            engine_clone=tmp_path,
            user_sid="S-1-5-21-TEST",
        )
        != ctx.name
    )


def test_idle_tick_retires_a_superseded_server_that_has_served_requests(tmp_path):
    """AC: a server whose served count is NONZERO and whose token is stale
    -- the population today's two mechanisms both miss -- retires without
    receiving a request, and is recorded as SUPERSEDED, not idle-demoted.

    The idle clock is freshly marked, so neither deadline arm can be what
    fires here.
    """
    skew.write_engine_stamp(tmp_path, "sha-before-publish")
    boot_token = skew.compute_client_token(tmp_path)

    ctx = server._ServerContext(
        name="pipe-superseded",
        sid="sid-x",
        version_state=_FakeVersionState(),
        engine_root=tmp_path,
        boot_token=boot_token,
    )
    ctx.telemetry.record_invocation(warm=True)
    assert ctx.telemetry.served_count() > 0
    idle.mark_invocation()

    calls = []
    monkey_target = idle.demote_if_idle

    def _capture(**kwargs):
        calls.append(kwargs)
        return True

    idle.demote_if_idle = _capture
    try:
        skew.write_engine_stamp(tmp_path, "sha-after-publish")
        ctx._idle_tick()
    finally:
        idle.demote_if_idle = monkey_target

    assert calls, "a superseded generation must reach demote_if_idle"
    assert calls[0]["token_stale"]() is True
    assert ctx.telemetry.snapshot()["exit_reason"] == telemetry.EXIT_REASON_SUPERSEDED


def test_healthy_server_is_not_retired_by_the_superseded_arm(tmp_path):
    """The other half of the AC: a current generation with a fresh idle
    clock keeps running. A predicate that retires healthy servers would
    destroy warm serving entirely, so this is pinned alongside the
    positive case."""
    skew.write_engine_stamp(tmp_path, "sha-current")
    boot_token = skew.compute_client_token(tmp_path)

    ctx = server._ServerContext(
        name="pipe-healthy",
        sid="sid-x",
        version_state=_FakeVersionState(),
        engine_root=tmp_path,
        boot_token=boot_token,
    )
    ctx.telemetry.record_invocation(warm=True)
    idle.mark_invocation()

    assert ctx._token_is_stale() is False
    assert (
        idle.should_demote(
            served_count=ctx.telemetry.served_count,
            token_stale=ctx._token_is_stale,
            idle_deadline_secs=900.0,
        )
        is False
    )


def test_token_staleness_read_failure_is_not_a_retirement_verdict(tmp_path):
    """Never raises, and an unreadable token reads as NOT stale -- the
    watchdog runs this every poll while a publish round may be mid-write,
    and a transient failure must degrade to today's behaviour (wait out
    the idle deadline), never to killing a healthy server."""
    ctx = server._ServerContext(
        name="pipe-x",
        sid="sid-x",
        version_state=_FakeVersionState(),
        engine_root=tmp_path,
        boot_token="some-token",
    )

    original = skew.compute_client_token

    def _boom(_root=None):
        raise OSError("stamp is mid-write")

    skew.compute_client_token = _boom
    try:
        assert ctx._token_is_stale() is False
    finally:
        skew.compute_client_token = original


def test_context_without_a_boot_token_disables_the_superseded_arm(tmp_path):
    """Every pre-existing `_ServerContext(...)` construction omits
    `boot_token`; those must keep exactly their old two-arm behaviour."""
    ctx = server._ServerContext(
        name="pipe-x", sid="sid-x", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    assert ctx.boot_token is None
    assert ctx._token_is_stale() is False


def test_boot_binds_the_pipes_own_token_as_the_boot_token():
    """The predicate must compare against the token actually EMBEDDED in
    the pipe name this server owns. Pinned by source inspection because
    the boot path binds a real pipe and does not return before serving
    forever: a refactor that recomputes a token for the context instead of
    passing the elected one would let the two drift silently.

    TARGETS THE FUNCTIONS THAT HOLD THE CODE, NOT `main()`. This read
    `inspect.getsource(server.main)` until 2026-08-21 and had been failing
    since `main()` became a thin wrapper over `_run_guarded` (the STEP 0
    crash-reporting guard) -- the invariant held the whole time, only the
    literal-source target was stale. `election.pipe_name` then moved once
    more, into `_elect_windows_pipe`, when the POSIX arm landed. A test
    pinned to source has to be repointed whenever the source moves; that
    is the cost of pinning something no live call can assert, and it is
    still cheaper than the silent drift it catches.
    """
    boot = inspect.getsource(server._run_guarded)
    assert "boot_token=token" in boot

    windows_arm = inspect.getsource(server._elect_windows_pipe)
    assert "name = election.pipe_name(token" in windows_arm
    # The token the pipe name is built from and the token handed to the
    # context must be the same object, not two derivations: one parameter,
    # threaded from `_run_guarded`'s single `compute_client_token` call.
    assert "token = skew.compute_client_token(repo_root)" in boot
    assert boot.count("compute_client_token") == 1


def test_evict_on_skew_drain_ordering_is_untouched_by_this_path():
    """Anti-scope pin: the superseded retirement adds a path, it does not
    reorder or weaken C16's respond -> close_listener -> drain sequence."""
    body = inspect.getsource(skew.evict_on_skew)
    assert body.index("respond(") < body.index("close_listener()") < body.index("drain()")
