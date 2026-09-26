"""The reproduction and acceptance oracle for the dispatch-ack reconcile
route: `warm.request_status` must not false-negative across held-frame,
executing, finished, restart and evicted cases.

Reproduces the 2026-09-01 double-execution incident (bug row
077d1a9a38b1) deterministically: a mutating frame held unread on one
connection while a second connection polls its key must answer
`not_received`, and once the held frame is finally released it must be
refused (tombstoned), never dispatched. See
`coordinator_core/contract/dispatch-ack-reconcile-contract.md` (D1-D5) and
`docs/plans/2026-09-23-warm-dispatch-reconcile.md` (C6).

Built on `test_server_loop.py`'s in-process `_FakeIO`/`_handle_connection`
harness (no real pipe, no spawn), extended to two SIMULTANEOUS connections
on their own threads -- the shape
`test_pool_serves_more_than_one_simultaneous_connection` uses for proving
more than one instance outstanding at once, applied here to threads rather
than real named pipes so the held-frame race is deterministic and
Windows/POSIX-portable.

Spec backlink: docs/plans/2026-09-23-warm-dispatch-reconcile.md (P188-C6).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence]


class _FakeIO:

    def __init__(self, lines, *, hold: threading.Event | None = None):
        self._lines = list(lines)
        self._hold = hold
        self.written: list[bytes] = []
        self.closed = False

    def readline(self) -> bytes:
        if self._hold is not None:
            self._hold.wait(timeout=5)
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


def _run_connection(io_obj, *, dispatch, in_flight=None):
    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=in_flight if in_flight is not None else server.InFlightCounter(),
        dispatch=dispatch,
    )


def _poll(key: str) -> dict:
    io_obj = _FakeIO([_frame(method="warm.request_status", extra={"params": {"key": key}})])
    _run_connection(io_obj, dispatch=lambda *_a, **_k: pytest.fail("poll must never reach dispatch"))
    [response] = _written(io_obj)
    return response["result"]


@pytest.fixture(autouse=True)
def _fresh_ack_store(monkeypatch):
    store = dispatch_ack.AckStore(boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)
    return store


def test_held_frame_polled_then_released_never_double_dispatches(_fresh_ack_store):
    key = "1-100"
    hold = threading.Event()
    handler_ran = threading.Event()

    def _dispatch(msg, *, caller=None, dispatch_key=None):
        handler_ran.set()
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_a = _FakeIO([_frame(id_="a", extra={"_dispatch_key": key})], hold=hold)
    in_flight_a = server.InFlightCounter()
    t_a = threading.Thread(target=_run_connection, kwargs=dict(io_obj=io_a, dispatch=_dispatch, in_flight=in_flight_a))
    t_a.start()

    time.sleep(0.1)
    assert not handler_ran.is_set()

    status = _poll(key)
    assert status["state"] == dispatch_ack.STATE_NOT_RECEIVED
    assert status["outcome"] == dispatch_ack.OUTCOME_NOT_DISPATCHED

    hold.set()
    t_a.join(timeout=5)

    assert not handler_ran.is_set()
    [response] = _written(io_a)
    assert response["error"]["code"] == server.DISPATCH_KEY_TOMBSTONED_ERROR

    assert _poll(key)["state"] == dispatch_ack.STATE_NOT_RECEIVED


def test_poll_during_execution_answers_executing(_fresh_ack_store):
    key = "1-200"
    release = threading.Event()
    entered = threading.Event()

    def _dispatch(msg, *, caller=None, dispatch_key=None):
        entered.set()
        release.wait(timeout=5)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_a = _FakeIO([_frame(id_="a", extra={"_dispatch_key": key})])
    t_a = threading.Thread(target=_run_connection, kwargs=dict(io_obj=io_a, dispatch=_dispatch))
    t_a.start()

    assert entered.wait(timeout=5)

    status = _poll(key)
    assert status["state"] == dispatch_ack.STATE_EXECUTING

    release.set()
    t_a.join(timeout=5)
    assert _written(io_a)[0]["result"] == "ok"


def test_poll_after_completion_answers_finished_with_outcome(_fresh_ack_store, monkeypatch):
    import coordinator_core.ipc as ipc_module
    from coordinator_core.authz.completion_evidence import evidence_class

    key = "1-300"
    method = "ceremony.commit_v2"

    async def _fake_dispatch_message(msg, *, caller=None, corr_id=None):
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": "ok"}

    monkeypatch.setattr(ipc_module, "dispatch_message", _fake_dispatch_message)

    io_a = _FakeIO([_frame(id_="a", method=method, extra={"_dispatch_key": key})])
    server._handle_connection(
        io_a,
        version_state=_FakeVersionState(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
    )
    assert _written(io_a)[0]["result"] == "ok"

    status = _poll(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT

    assert evidence_class(method) is not None


def test_poll_after_engine_restart_answers_unknowable_engine_restarted(monkeypatch):
    key = "1-400"
    restarted_store = dispatch_ack.AckStore(boot_ns=1_000)
    monkeypatch.setattr(server, "_ack_store", restarted_store)

    status = _poll(key)
    assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
    assert status["reason"] == dispatch_ack.REASON_ENGINE_RESTARTED


def test_poll_of_an_evicted_key_answers_unknowable_expired(monkeypatch):
    store = dispatch_ack.AckStore(capacity=2, boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)

    assert store.admit("1-500", "ceremony.commit_v2")
    assert store.admit("1-501", "ceremony.commit_v2")
    assert store.admit("1-502", "ceremony.commit_v2")

    status = _poll("1-500")
    assert status["state"] == dispatch_ack.STATE_UNKNOWABLE
    assert status["reason"] == dispatch_ack.REASON_EXPIRED

    status_survivor = _poll("1-501")
    assert status_survivor["state"] == dispatch_ack.STATE_EXECUTING


def test_cold_handler_answers_unknowable_no_resident_engine():
    from coordinator_core.ops.warm_request_status import _warm_request_status

    result = _warm_request_status({"key": "1-600"})

    assert result["state"] == dispatch_ack.STATE_UNKNOWABLE
    assert result["reason"] == dispatch_ack.REASON_NO_RESIDENT_ENGINE
    assert result["state"] != dispatch_ack.STATE_NOT_RECEIVED
