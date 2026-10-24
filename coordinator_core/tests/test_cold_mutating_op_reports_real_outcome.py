"""A cold (IN_PROCESS) mutating op that outlives the dispatch backstop returns its real outcome."""

from __future__ import annotations

import asyncio
import time

import coordinator_core.ipc as ipc
from coordinator_core import dispatch_abandon
from coordinator_core.telemetry import op_latency

_OP = "ceremony.fake_cold_mutating"


def _dispatch(monkeypatch, handler, route, op=_OP):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.1")
    monkeypatch.setenv(op_latency.ROUTE_ENV, route)
    monkeypatch.setitem(ipc._REGISTRY, op, handler)
    monkeypatch.setattr(ipc, "_timeout_for", lambda m, msg=None: 0.1)
    import coordinator_core.warm.client as wc

    monkeypatch.setattr(wc, "_op_may_mutate", lambda m: True)
    overruns = []
    monkeypatch.setattr(ipc, "_report_ceremony_overrun", lambda *a: overruns.append(a))
    msg = {"jsonrpc": "2.0", "id": 1, "method": op, "params": {}}
    return asyncio.run(ipc._dispatch_message_impl(msg)), overruns


def test_cold_mutating_returns_handler_result(monkeypatch):
    seen = {}

    def slow(params, ctx=None, repo_root=None):
        seen["remaining"] = dispatch_abandon.remaining()
        seen["active_set_at_start"] = dispatch_abandon.ACTIVE.get().is_set()
        time.sleep(0.3)
        seen["abandoned_at_end"] = dispatch_abandon.ACTIVE.get().is_set()
        return {"ok": True}

    env, overruns = _dispatch(monkeypatch, slow, op_latency.IN_PROCESS)
    assert "error" not in env
    assert env["result"] == {"ok": True}
    diag = env["data"]["diagnosis"]
    assert diag["case"] == "completed_after_backstop"
    assert diag["op"] == _OP
    assert diag["backstop_secs"] == 0.1
    assert diag["waited_secs"] > 0.1
    assert seen["remaining"] is None
    assert seen["abandoned_at_end"] is False
    assert len(overruns) == 1


def test_cold_mutating_handler_error_is_its_own_error(monkeypatch):
    def boom(params, ctx=None, repo_root=None):
        time.sleep(0.3)
        raise RuntimeError("deliberate")

    env, _ = _dispatch(monkeypatch, boom, op_latency.IN_PROCESS)
    assert env["error"]["code"] != -32004
    assert "deliberate" in env["error"]["message"]
    assert env["error"]["data"]["diagnosis"]["case"] == "completed_after_backstop"


def test_cold_mutating_hung_past_ceiling_reports_may_have_completed(monkeypatch):
    def hung(params, ctx=None, repo_root=None):
        time.sleep(0.9)
        return {"ok": True}

    env, _ = _dispatch(monkeypatch, hung, op_latency.IN_PROCESS)
    assert "result" not in env
    assert env["error"]["code"] == -32004
    assert "reconcile before any retry" in env["error"]["message"]
    assert "may have completed" in env["error"]["message"]
    diag = env["error"]["data"]["diagnosis"]
    assert diag["case"] == "hung_past_ceiling"
    assert diag["backstop_secs"] == 0.1
    assert diag["ceiling_secs"] == 0.1 * ipc._COLD_AWAIT_CEILING_FACTOR
    assert 0.1 < diag["waited_secs"] < 0.9


def test_warm_route_still_reports_indeterminate(monkeypatch):
    def slow(params, ctx=None, repo_root=None):
        time.sleep(0.3)
        return {"ok": True}

    env, _ = _dispatch(monkeypatch, slow, op_latency.WARM_SERVER)
    assert env["error"]["code"] == -32004
    assert "result" not in env
