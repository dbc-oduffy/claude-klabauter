"""The cold-await path records per-attempt process and wall time in the diagnosis it already returns."""

from __future__ import annotations

import asyncio
import time

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.telemetry import op_latency

pytestmark = pytest.mark.complete

_OP = "ceremony.fake_cold_attempts"
_KEYS = {"phase", "outcome", "process_ms", "wall_ms"}


def _dispatch(monkeypatch, handler, route=op_latency.IN_PROCESS):
    monkeypatch.setenv(op_latency.ROUTE_ENV, route)
    monkeypatch.setitem(ipc._REGISTRY, _OP, handler)
    monkeypatch.setattr(ipc, "_timeout_for", lambda m, msg=None: 0.1)
    import coordinator_core.warm.client as wc

    monkeypatch.setattr(wc, "_op_may_mutate", lambda m: True)
    monkeypatch.setattr(ipc, "_report_ceremony_overrun", lambda *a: None)
    msg = {"jsonrpc": "2.0", "id": 1, "method": _OP, "params": {}}
    return asyncio.run(ipc._dispatch_message_impl(msg))


def test_completed_after_backstop_carries_both_attempts(monkeypatch):
    def slow(params, ctx=None, repo_root=None):
        time.sleep(0.3)
        return {"ok": True}

    env = _dispatch(monkeypatch, slow)
    attempts = env["data"]["diagnosis"]["attempts"]
    assert [a["phase"] for a in attempts] == ["backstop", "await_ceiling"]
    assert [a["outcome"] for a in attempts] == ["elapsed", "completed"]
    assert all(set(a) == _KEYS for a in attempts)
    assert attempts[0]["wall_ms"] >= 90.0


def test_sleeping_handler_shows_wall_far_above_process_ms(monkeypatch):
    def idle(params, ctx=None, repo_root=None):
        time.sleep(0.3)
        return {"ok": True}

    first = _dispatch(monkeypatch, idle)["data"]["diagnosis"]["attempts"][0]
    assert first["wall_ms"] > first["process_ms"] + 50.0


def test_hung_past_ceiling_reports_the_hung_attempt(monkeypatch):
    def hung(params, ctx=None, repo_root=None):
        time.sleep(0.9)
        return {"ok": True}

    env = _dispatch(monkeypatch, hung)
    assert env["error"]["code"] == -32004
    attempts = env["error"]["data"]["diagnosis"]["attempts"]
    assert [a["outcome"] for a in attempts] == ["elapsed", "hung"]
    assert attempts[1]["phase"] == "await_ceiling"


def test_handler_error_after_backstop_reports_a_raised_attempt(monkeypatch):
    def boom(params, ctx=None, repo_root=None):
        time.sleep(0.3)
        raise RuntimeError("deliberate")

    env = _dispatch(monkeypatch, boom)
    attempts = env["error"]["data"]["diagnosis"]["attempts"]
    assert [a["outcome"] for a in attempts] == ["elapsed", "raised"]


def test_an_op_inside_its_backstop_records_no_attempts(monkeypatch):
    def quick(params, ctx=None, repo_root=None):
        return {"ok": True}

    env = _dispatch(monkeypatch, quick)
    assert env["result"] == {"ok": True}
    assert "data" not in env


def test_cold_attempt_spawns_nothing(monkeypatch):
    import subprocess

    def refuse(*a, **k):
        raise AssertionError("cold attempt record spawned a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    rec = ipc._cold_attempt("backstop", time.monotonic(), time.process_time(), "elapsed")
    assert set(rec) == _KEYS
