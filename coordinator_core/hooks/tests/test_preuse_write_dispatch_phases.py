"""hooks.preuse_write_dispatch: shared phases and guards named on the row; stops at the hook deadline."""

from __future__ import annotations

import time

import pytest

from coordinator_core.hooks import _preuse_phases as pp
from coordinator_core.hooks import preuse_write_dispatch as mod
from coordinator_core.telemetry import op_row_fields
from coordinator_core.write_guards import engine

_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                "permissionDecisionReason": "no"}}
_PAYLOAD = {"payload": {"tool_name": "Edit", "tool_input": {"file_path": "x"}, "session_id": "", "cwd": "."}}


def _guard(name, cls, priority, fn):
    return engine._Guard(name, cls, ["Edit"], priority, fn)


def _drive(monkeypatch, guards, door_s=2.1):
    monkeypatch.setattr(pp, "_door_deadline_s", lambda: door_s)
    monkeypatch.setattr(engine, "_discover_guards", lambda: (guards, []))
    monkeypatch.setattr(engine, "_apply_level", lambda name, out, session_id, cwd: out)
    token = op_row_fields.begin()
    try:
        out = mod._handler(_PAYLOAD)
    finally:
        fields = op_row_fields.drain(token)
    return out, fields


def _sleeper(ran, name, secs, result=None):
    def check(payload):
        ran.append(name)
        time.sleep(secs)
        return result

    return check


def test_slow_guard_is_named_and_later_guards_do_not_run(monkeypatch):
    ran: list[str] = []
    guards = [
        _guard("slow_guard", "advisory", 1, _sleeper(ran, "slow_guard", 0.2)),
        _guard("later_guard", "advisory", 2, _sleeper(ran, "later_guard", 0.0, _DENY)),
    ]
    out, fields = _drive(monkeypatch, guards, door_s=2.1)
    assert ran == ["slow_guard"]
    assert out == pp.no_advisory()
    assert fields["slow_phase"] == "slow_guard"
    assert fields["deadline_stop"] == "later_guard"
    assert fields["spans"][0]["wall_ms"] >= 150
    assert fields["spans"][0]["cpu_ms"] < 50


def test_within_deadline_every_guard_runs_and_a_deny_is_returned(monkeypatch):
    ran: list[str] = []
    guards = [
        _guard("quick_guard", "advisory", 1, _sleeper(ran, "quick_guard", 0.0)),
        _guard("deny_guard", "hard-deny", 0, _sleeper(ran, "deny_guard", 0.0, _DENY)),
    ]
    out, fields = _drive(monkeypatch, guards, door_s=60.0)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "deadline_stop" not in fields
    assert ran == ["deny_guard"]


def test_deny_found_before_the_deadline_is_kept(monkeypatch):
    ran: list[str] = []
    guards = [
        _guard("deny_guard", "hard-deny", 0, _sleeper(ran, "deny_guard", 0.2, _DENY)),
        _guard("later_guard", "advisory", 1, _sleeper(ran, "later_guard", 0.0)),
    ]
    out, fields = _drive(monkeypatch, guards, door_s=2.1)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ran == ["deny_guard"]
    assert "deadline_stop" not in fields


def test_shared_phases_are_legs_on_the_row(monkeypatch):
    guards = [_guard("g", "advisory", 1, lambda payload: None)]
    monkeypatch.setattr(pp, "_door_deadline_s", lambda: 60.0)
    monkeypatch.setattr(engine, "_discover_guards", lambda: (time.sleep(0.07) or guards, []))
    token = op_row_fields.begin()
    try:
        mod._handler(_PAYLOAD)
    finally:
        fields = op_row_fields.drain(token)
    assert fields["slow_phase"] == "guard_discovery"


def test_engine_check_deadline_is_inert_when_phases_module_unloaded(monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "coordinator_core.hooks._preuse_phases", raising=False)
    engine._check_deadline("any")
