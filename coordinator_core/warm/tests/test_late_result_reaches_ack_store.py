"""A warm-served mutating handler that outlives its budget delivers its real outcome to the ack record."""

from __future__ import annotations

import time

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.telemetry import op_latency
from coordinator_core.warm import dispatch_ack, server

_OP = "ceremony.fake_warm_mutating"
_BUDGET = 0.1


@pytest.fixture
def op_env(monkeypatch):
    monkeypatch.setenv(op_latency.ROUTE_ENV, op_latency.WARM_SERVER)
    monkeypatch.setattr(ipc, "_timeout_for", lambda m, msg=None: _BUDGET)
    import coordinator_core.warm.client as wc

    monkeypatch.setattr(wc, "_op_may_mutate", lambda m: True)
    monkeypatch.setattr(ipc, "_report_ceremony_overrun", lambda *a: None)
    monkeypatch.setattr(server, "_ack_store", dispatch_ack.AckStore())

    def register(handler):
        monkeypatch.setitem(ipc._REGISTRY, _OP, handler)

    return register


def _msg():
    return {"jsonrpc": "2.0", "id": 5, "method": _OP, "params": {}}


def _slow_sha(params, ctx=None, repo_root=None):
    time.sleep(_BUDGET * 3)
    return {"sha": "abc123", "_scope_touch_paths": ["x"]}


def _slow_raise(params, ctx=None, repo_root=None):
    time.sleep(_BUDGET * 3)
    raise RuntimeError("landed badly")


def _admit() -> str:
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, _OP)
    return key


def _stamp_from(response, key):
    server._stamp_pool_future(key, _DoneFuture(response))
    return server._ack_store.status(key)


class _DoneFuture:
    def __init__(self, result):
        self._result = result

    def cancelled(self):
        return False

    def exception(self):
        return None

    def result(self):
        return self._result


def test_pool_body_returns_the_late_result_and_the_record_finishes(op_env):
    op_env(_slow_sha)
    key = _admit()
    response = server._pool_dispatch_worker_body(_msg(), None)
    assert response["result"] == {"sha": "abc123"}
    status = _stamp_from(response, key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT
    assert status["result"]["sha"] == "abc123"


def test_pool_body_late_raise_stamps_error(op_env):
    op_env(_slow_raise)
    key = _admit()
    response = server._pool_dispatch_worker_body(_msg(), None)
    assert "landed badly" in response["error"]["message"]
    status = _stamp_from(response, key)
    assert status["outcome"] == dispatch_ack.OUTCOME_ERROR


def test_inline_dispatch_stamps_the_late_result_not_the_timeout(op_env):
    op_env(_slow_sha)
    key = _admit()
    response = server._run_dispatch(_msg(), dispatch_key=key)
    assert response["error"]["code"] == -32004
    status = server._ack_store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED
    assert status["result"]["sha"] == "abc123"


def test_inline_dispatch_late_raise_stamps_error(op_env):
    op_env(_slow_raise)
    key = _admit()
    response = server._run_dispatch(_msg(), dispatch_key=key)
    assert response["error"]["code"] == -32004
    assert server._ack_store.status(key)["outcome"] == dispatch_ack.OUTCOME_ERROR


def test_handler_inside_budget_reply_is_unchanged_and_not_late(op_env):
    op_env(lambda params, ctx=None, repo_root=None: {"sha": "fast"})
    key = _admit()
    response = server._pool_dispatch_worker_body(_msg(), None)
    assert response["result"] == {"sha": "fast"}
    assert "error" not in response
    assert _stamp_from(response, key)["result"]["sha"] == "fast"


def test_sink_fires_only_when_the_handler_finishes_after_the_timeout_envelope():
    seen: list = []
    inside = ipc.LateCompletion(sink=seen.append)
    inside.complete(value={"sha": "in"})
    assert seen == [] and inside.late_envelope() is None

    late = ipc.LateCompletion(sink=seen.append)
    late.mark_timed_out(9)
    assert seen == []
    late.complete(value={"sha": "late"})
    assert seen == [{"jsonrpc": "2.0", "id": 9, "result": {"sha": "late"}}]


def test_a_late_result_supersedes_an_abandoned_stamp_but_not_a_result():
    store = dispatch_ack.AckStore()
    key = dispatch_ack.mint_key()
    store.admit(key, _OP)
    store.stamp(key, dispatch_ack.OUTCOME_ABANDONED)
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"sha": "first"})
    store.stamp(key, dispatch_ack.OUTCOME_RESULT, result={"sha": "second"})
    assert store.status(key)["result"] == {"sha": "first"}
