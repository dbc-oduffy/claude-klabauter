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
"""

from __future__ import annotations

import json
import threading

import pytest

from coordinator_core.session import declared_writes
from coordinator_core.warm import lifecycle, server, skew


@pytest.fixture(autouse=True)
def _reset_shutdown_guard():
    """`warm.lifecycle`'s single-shot shutdown guard is process-global
    (module docstring: "must be entered at most once per server life").
    Reset around every test so one test's `drain_and_exit` call does not
    silently no-op the next test's."""
    lifecycle.reset_shutdown_guard_for_test()
    yield
    lifecycle.reset_shutdown_guard_for_test()


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


def _frame(*, id_, method="noop", extra=None) -> bytes:
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": {}}
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

    def _dispatch(msg: dict) -> dict:
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

    def _dispatch(msg: dict) -> dict:
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


def test_run_dispatch_opens_per_request_state(monkeypatch):
    """`_run_dispatch` must open `entry_seam.per_request_state()` around
    every call into `coordinator_core.ipc.dispatch_message` -- a fresh,
    empty declared-writes collection per call, closed again on return."""
    captured: dict[str, object] = {}

    async def fake_dispatch_message(msg: dict) -> dict:
        captured["active_during"] = list(declared_writes.active_declarations() or [])
        declared_writes.declare_write("inner.txt")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": "ok"}

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", fake_dispatch_message)

    assert declared_writes.active_declarations() is None
    result = server._run_dispatch({"jsonrpc": "2.0", "id": 1, "method": "noop", "params": {}})

    assert result["result"] == "ok"
    assert captured["active_during"] == []
    assert declared_writes.active_declarations() is None
