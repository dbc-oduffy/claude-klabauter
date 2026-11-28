"""A warm-engine stop and restart while `dispatch.measured_rev` is in flight yields the op result from
the restarted engine, and the stop reports the in-flight count before it signals anything.

Drives the default client call (`client.try_warm_dispatch`) against two generations of the real server
connection layer (`server._handle_connection` -> `_serve_line`, real `AckStore`, real
`warm.request_status` intercept, real `engine.stop_probe` outstanding report) over an in-memory pipe.
Generation 1 holds the op, the stop reads its outstanding count through `pair_stop`, then generation 1
dies and generation 2 (a fresh `AckStore`) answers the client's poll and the one re-send.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client, dispatch_ack, pair_stop, server

_OP = "dispatch.measured_rev"
_SHA = "a" * 40
_KILLED = object()
_DEADLINE_SECS = 30.0
_ENTRY_SECS = 5.0


def _await_next_clock_tick() -> None:
    """invariant: dispatch keys and `AckStore.boot_ns` share one monotonic clock that ticks coarsely on
    Windows; a key and a boot in the same tick read as `expired`, never `engine-restarted`."""
    start = time.monotonic_ns()
    while time.monotonic_ns() <= start:
        time.sleep(0.001)


class _Version:
    server_sha = "restart-e2e"
    last_skew_axes = ()

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _ServerEnd:
    def __init__(self, frame: bytes, replies: "queue.Queue") -> None:
        self._frame = frame
        self._replies = replies

    def readline(self) -> bytes:
        frame, self._frame = self._frame, b""
        return frame

    def write(self, data: bytes) -> None:
        self._replies.put(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _ClientEnd:
    def __init__(self, engine: "_Engine") -> None:
        self._engine = engine
        self._payload = b""
        self._replies: queue.Queue = queue.Queue()

    def write(self, data: bytes) -> None:
        self._payload += data

    def flush(self) -> None:
        self._engine.accept(self._payload, self._replies)
        self._payload = b""

    def readline(self) -> bytes:
        item = self._replies.get()
        if item is _KILLED:
            raise BrokenPipeError("the engine died with this connection open")
        return item

    def close(self) -> None:
        pass


class _Engine:
    """One server generation: real connection handling, a stand-in op body."""

    def __init__(self, ctx: "server._ServerContext", op_body) -> None:
        self.ctx = ctx
        self.op_body = op_body
        self.ops_seen: list[str] = []
        self._open: list[queue.Queue] = []

    def _dispatch(self, msg: dict, *, caller=None, dispatch_key=None) -> dict:
        self.ops_seen.append(msg["method"])
        return self.op_body(msg)

    def accept(self, payload: bytes, replies: "queue.Queue") -> None:
        self._open.append(replies)
        threading.Thread(
            target=server._handle_connection,
            args=(_ServerEnd(payload, replies),),
            kwargs={
                "version_state": _Version(),
                "server_sha": _Version.server_sha,
                "close_listener": lambda: None,
                "drain": lambda: None,
                "in_flight": self.ctx.in_flight,
                "dispatch": self._dispatch,
                "outstanding": self.ctx.drain_outstanding,
            },
            daemon=True,
        ).start()

    def kill(self) -> None:
        for replies in self._open:
            if replies.empty():
                replies.put(_KILLED)


class _Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._tmp = tmp_path
        self._monkeypatch = monkeypatch
        self.current: _Engine = self.boot(self._hold)

    def _hold(self, msg: dict) -> dict:
        self.entered.set()
        self.release.wait(_DEADLINE_SECS)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"sha": "never-delivered"}}

    @staticmethod
    def _answer(msg: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"sha": _SHA, "run_id": msg["params"]["run_id"]}}

    def boot(self, op_body) -> _Engine:
        _await_next_clock_tick()
        store = dispatch_ack.AckStore()
        self._monkeypatch.setattr(server, "_ack_store", store)
        _await_next_clock_tick()
        ctx = server._ServerContext(
            name="pipe-restart-e2e", sid="sid-restart-e2e", version_state=_Version(), engine_root=self._tmp
        )
        return _Engine(ctx, op_body)

    def restart(self) -> _Engine:
        previous = self.current
        self.current = self.boot(self._answer)
        previous.kill()
        return self.current


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "runtime"))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "restart-e2e-token")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: "bridge")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: True)
    monkeypatch.setattr(client, "_delivering_engine", lambda: {"pid": os.getpid()})
    monkeypatch.setattr(client, "_abandoned_in_import", lambda key: False)
    monkeypatch.setattr(client, "_journaled_outcome", lambda key: None)
    built = _Rig(tmp_path, monkeypatch)
    monkeypatch.setattr(client, "_open_pipe", lambda name: _ClientEnd(built.current))
    yield built
    built.release.set()


def _measured_rev_msg() -> dict:
    return {"jsonrpc": "2.0", "id": 41, "method": _OP, "params": {"run_id": "restart-e2e-run", "paths": []}}


def test_measured_rev_in_flight_across_a_stop_and_restart_returns_its_result_after_the_report(
    rig: _Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = rig.current
    outcome: dict = {}
    caller = threading.Thread(
        target=lambda: outcome.setdefault("response", client.try_warm_dispatch(_measured_rev_msg())), daemon=True
    )
    caller.start()
    assert rig.entered.wait(_ENTRY_SECS), f"the op never reached the first engine: {outcome!r}"

    monkeypatch.setattr(pair_stop, "_open_pipe", lambda pipe: _ClientEnd(first))
    reports: list = []
    order: list[str] = []

    def _on_in_flight(count) -> None:
        order.append("report")
        reports.append(count)

    pair_stop.stop_warm_pair(tmp_path, server_pipe="bridge", on_in_flight=_on_in_flight)
    order.append("stop-returned")
    assert order == ["report", "stop-returned"]
    assert reports and reports[0] is not None and reports[0] >= 1, "the stop's report missed the in-flight op"
    assert caller.is_alive(), "the op was answered before the engine was restarted"

    second = rig.restart()
    caller.join(_DEADLINE_SECS)
    assert not caller.is_alive(), "the caller never got an answer from the restarted engine"

    response = outcome["response"]
    assert response is not None and "error" not in response, (response, first.ops_seen, second.ops_seen)
    assert response["result"]["sha"] == _SHA
    assert second.ops_seen == [_OP]
    assert first.ops_seen == [_OP]


def test_a_failed_re_send_after_the_restart_does_not_loop(rig: _Rig) -> None:
    first = rig.current
    outcome: dict = {}
    caller = threading.Thread(
        target=lambda: outcome.setdefault("response", client.try_warm_dispatch(_measured_rev_msg())), daemon=True
    )
    caller.start()
    assert rig.entered.wait(_DEADLINE_SECS)

    def _killed_again(msg: dict) -> dict:
        second.kill()
        threading.Event().wait(_DEADLINE_SECS)
        return {}

    second = rig.boot(_killed_again)
    rig.current = second
    first.kill()
    caller.join(_DEADLINE_SECS)

    assert not caller.is_alive()
    assert outcome["response"]["error"]["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert second.ops_seen == [_OP], (outcome["response"], second.ops_seen)
