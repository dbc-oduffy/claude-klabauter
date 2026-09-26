
from __future__ import annotations

import concurrent.futures
import json

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence]


class _FakeIO:
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
    def __init__(self, *, skewed: bool = False):
        self._skewed = skewed

    def is_skewed(self, client_token: str) -> bool:
        return self._skewed


def _frame(*, id_=1, method="ceremony.commit_v2", extra=None, token="client-token") -> bytes:
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": {}}
    if token is not None:
        msg["_engine_token"] = token
    if extra:
        msg.update(extra)
    return (json.dumps(msg) + "\n").encode("utf-8")


def _written(io_obj: _FakeIO) -> list[dict]:
    return [json.loads(line) for line in io_obj.written]


def _run_serve_line(io_obj, *, dispatch, version_state=None):
    server._handle_connection(
        io_obj,
        version_state=version_state or _FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        dispatch=dispatch,
    )


@pytest.fixture(autouse=True)
def _fresh_ack_store(monkeypatch):
    store = dispatch_ack.AckStore(boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)
    return store


def test_request_status_is_answered_without_reaching_dispatch(_fresh_ack_store):
    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("warm.request_status must never reach dispatch()")

    io_obj = _FakeIO([_frame(method="warm.request_status", extra={"params": {"key": "1-500"}})])
    _run_serve_line(io_obj, dispatch=_must_not_run)

    [response] = _written(io_obj)
    assert response["result"]["state"] == dispatch_ack.STATE_NOT_RECEIVED
    assert "engine_boot_ns" in response["result"]
    assert "engine_pid" in response["result"]


def test_request_status_reflects_an_admitted_key(_fresh_ack_store):
    _fresh_ack_store.admit("1-500", "ceremony.commit_v2")

    def _noop(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not reach dispatch")

    io_obj = _FakeIO([_frame(method="warm.request_status", extra={"params": {"key": "1-500"}})])
    _run_serve_line(io_obj, dispatch=_noop)

    [response] = _written(io_obj)
    assert response["result"]["state"] == dispatch_ack.STATE_EXECUTING


def test_a_keyed_mutating_frame_is_admitted_before_dispatch_runs(_fresh_ack_store):
    seen_key = {}

    def _dispatch(msg, *, caller=None, dispatch_key=None):
        seen_key["key"] = dispatch_key
        assert _fresh_ack_store.status(dispatch_key)["state"] == dispatch_ack.STATE_EXECUTING
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_obj = _FakeIO([_frame(method="ceremony.commit_v2", extra={"_dispatch_key": "1-777"})])
    _run_serve_line(io_obj, dispatch=_dispatch)

    assert seen_key["key"] == "1-777"
    assert _written(io_obj)[0]["result"] == "ok"


def test_a_replayed_key_is_refused_with_the_tombstone_code_never_dispatched(_fresh_ack_store):
    _fresh_ack_store.admit("1-777", "ceremony.commit_v2")

    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("a key already in the store must never redispatch")

    io_obj = _FakeIO([_frame(method="ceremony.commit_v2", extra={"_dispatch_key": "1-777"})])
    _run_serve_line(io_obj, dispatch=_must_not_run)

    [response] = _written(io_obj)
    assert response["error"]["code"] == server.DISPATCH_KEY_TOMBSTONED_ERROR


def test_an_unkeyed_frame_is_dispatched_exactly_as_today_and_recorded_nowhere(_fresh_ack_store):
    calls = []

    def _dispatch(msg, *, caller=None):
        calls.append(msg["id"])
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_obj = _FakeIO([_frame(method="ceremony.commit_v2")])
    _run_serve_line(io_obj, dispatch=_dispatch)

    assert calls == [1]
    assert _written(io_obj)[0]["result"] == "ok"


def test_a_compute_only_method_is_never_admitted(_fresh_ack_store, monkeypatch):
    monkeypatch.setattr(server, "_op_may_mutate", lambda method: False)
    calls = []

    def _dispatch(msg, *, caller=None, dispatch_key=None):
        calls.append(dispatch_key)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_obj = _FakeIO([_frame(method="ping", extra={"_dispatch_key": "1-999"})])
    _run_serve_line(io_obj, dispatch=_dispatch)

    assert _written(io_obj)[0]["result"] == "ok"
    assert _fresh_ack_store.status("1-999")["state"] == dispatch_ack.STATE_NOT_RECEIVED


def test_settings_home_refusal_stamps_not_dispatched_handler_never_ran(_fresh_ack_store, monkeypatch):
    from coordinator_core.warm.caller_context import CallerContext

    handler_ran = []

    def _fake_refusal(request_id, claim, resolved):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32008, "message": "mismatch"}}

    monkeypatch.setattr(server, "_settings_home_refusal", _fake_refusal)
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: "/served/home"
    )

    caller = CallerContext(
        plugin_root=None, cwd="/x", session_id=None, agent_id=None,
        pid=None, env=None, settings_home="/claimed/home",
    )

    out = server._run_dispatch({"id": 1, "method": "ceremony.commit_v2"}, caller=caller, dispatch_key="1-321")

    assert out["error"]["code"] == -32008
    assert handler_ran == []
    status = _fresh_ack_store.status("1-321")
    assert status == {"state": dispatch_ack.STATE_NOT_RECEIVED, "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED}
    assert _fresh_ack_store.admit("1-321", "ceremony.commit_v2") is False


class _DoneFuture:

    def __init__(self, *, result=None, exception=None, cancelled=False):
        self._result = result
        self._exception = exception
        self._cancelled = cancelled

    def result(self, timeout=None):
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self, timeout=None):
        return self._exception

    def cancelled(self):
        return self._cancelled


def test_pool_worker_marker_stamps_not_dispatched_and_marker_never_reaches_the_wire(_fresh_ack_store):
    _fresh_ack_store.admit("1-555", "ceremony.commit_v2")
    envelope = {"jsonrpc": "2.0", "id": 1, "result": "ok", server._NOT_DISPATCHED_MARKER: True}

    server._stamp_pool_future("1-555", _DoneFuture(result=envelope))

    assert server._NOT_DISPATCHED_MARKER not in envelope
    status = _fresh_ack_store.status("1-555")
    assert status == {"state": dispatch_ack.STATE_NOT_RECEIVED, "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED}
    assert _fresh_ack_store.admit("1-555", "ceremony.commit_v2") is False


class _CancellableFuture:
    def __init__(self):
        self.cancel_called = False
        self._callbacks = []

    def result(self, timeout=None):
        raise concurrent.futures.TimeoutError()

    def cancel(self):
        self.cancel_called = True
        return True

    def add_done_callback(self, fn):
        self._callbacks.append(fn)
        fn(self)

    def cancelled(self):
        return True

    def exception(self, timeout=None):
        raise concurrent.futures.CancelledError()


class _Pool:
    def __init__(self, future):
        self.future = future

    def submit(self, *_a, **_k):
        return self.future


def test_successful_cancel_stamps_not_dispatched_never_executing(_fresh_ack_store, monkeypatch):
    _fresh_ack_store.admit("1-888", "ceremony.commit_v2")
    fut = _CancellableFuture()

    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: _Pool(fut), raising=False)

    out = ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ceremony.commit_v2"}, dispatch_key="1-888")

    assert fut.cancel_called
    status = _fresh_ack_store.status("1-888")
    assert status["state"] != dispatch_ack.STATE_EXECUTING
    assert status == {"state": dispatch_ack.STATE_NOT_RECEIVED, "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED}


def test_ipc_internal_op_timeout_stamps_abandoned_via_run_dispatch(_fresh_ack_store, monkeypatch):
    _fresh_ack_store.admit("1-111", "ceremony.commit_v2")

    async def _fake_dispatch_message(msg, *, caller=None, corr_id=None):
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {"code": server.WARM_DISPATCH_INDETERMINATE, "message": "op timeout"},
        }

    monkeypatch.setattr(server, "dispatch_message", _fake_dispatch_message, raising=False)
    import coordinator_core.ipc as ipc_module

    monkeypatch.setattr(ipc_module, "dispatch_message", _fake_dispatch_message)

    out = server._run_dispatch({"id": 1, "method": "ceremony.commit_v2"}, caller=None, dispatch_key="1-111")

    assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    status = _fresh_ack_store.status("1-111")
    assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
    assert status["state"] != dispatch_ack.STATE_FINISHED
    assert status["state"] != dispatch_ack.STATE_NOT_RECEIVED


def test_broken_process_pool_stamps_worker_lost_for_a_mutating_op(_fresh_ack_store, monkeypatch):
    _fresh_ack_store.admit("1-222", "ceremony.commit_v2")

    class _BrokenPool:
        def submit(self, *_a, **_k):
            from concurrent.futures.process import BrokenProcessPool

            raise BrokenProcessPool("dead worker")

    ctx = server._ServerContext.__new__(server._ServerContext)
    import threading as _threading

    ctx._dispatch_pool = _BrokenPool()
    ctx._dispatch_pool_lock = _threading.Lock()
    ctx._pool_outstanding = server.InFlightCounter()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: ctx._dispatch_pool, raising=False)
    monkeypatch.setattr(server, "_run_dispatch", lambda *_a, **_k: pytest.fail("must not re-run a mutation"))

    out = ctx._pool_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ceremony.commit_v2"}, dispatch_key="1-222")

    assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    status = _fresh_ack_store.status("1-222")
    assert status == {"state": dispatch_ack.STATE_UNKNOWABLE, "reason": dispatch_ack.OUTCOME_WORKER_LOST}
