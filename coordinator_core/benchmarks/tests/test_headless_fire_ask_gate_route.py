"""The headless-fire ask_gate route benchmark classifies route and error code and never hangs.

Behaviour pinned: a spawn of the cold-operation module is `cold` and its absence is `warm`; engine error
envelopes, subprocess timeouts and other exceptions map to their error codes; the production entry
point (`measure`) bills a real `_invoke_source` call, reports a bounded-timeout call as
`error_code='timeout'` on the cold route with process time and a spawn, instead of hanging.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.benchmarks import headless_fire_ask_gate_route as bench

pytestmark = pytest.mark.cadence


def test_route_is_cold_only_when_cold_operation_is_spawned():
    cold = [["python", "-m", bench.COLD_MODULE, "req.json", "resp.json"]]
    assert bench.classify_route(cold) == "cold"
    assert bench.classify_route([]) == "warm"
    assert bench.classify_route([["git", "status"]]) == "warm"


def test_error_codes():
    assert bench.error_code_of(None) is None
    assert bench.error_code_of(subprocess.TimeoutExpired("x", 1)) == "timeout"
    assert bench.error_code_of(RuntimeError(json.dumps({"code": -32004, "message": "hung"}))) == -32004
    assert bench.error_code_of(RuntimeError("plain")) == "RuntimeError"
    assert bench.error_code_of(ValueError("v")) == "ValueError"


def test_bounded_call_reports_timeout_on_cold_route(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    report = bench.measure(timeout=0.05)
    assert report["route"] == "cold"
    assert report["error_code"] == "timeout"
    assert report["spawns"] >= 1
    assert report["process_ms"] > 0
    assert set(report) >= {"route", "process_ms", "spawns", "error_code"}
