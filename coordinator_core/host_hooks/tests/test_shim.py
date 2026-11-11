"""Shim: engine verdict, neutral fallback on down/slow/error, backstop deny, degrade rows, fail-open."""
from __future__ import annotations

import io
import json
import time

import pytest

from coordinator_core.bash_guards import block_push_pr_non_owned_remote as push_guard
from coordinator_core.host_hooks import shim
from coordinator_core.warm import telemetry

CODEX_LS = {"hook_event_name": "PreToolUse", "session_id": "sess-1", "cwd": "/nonexistent-shim-cwd",
            "tool_name": "exec_command", "tool_input": {"command": "ls"}}
CODEX_PUSH = {**CODEX_LS, "tool_input": {"command": "git push https://github.com/stranger/r.git main"}}
AGY_LS = {"conversationId": "agy-1", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
AGY_PUSH = {"conversationId": "agy-1", "toolCall": {"name": "run_command", "args": {
    "CommandLine": "git push https://github.com/stranger/r.git main"}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    rows = []
    monkeypatch.setattr(telemetry, "record_degrade", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def rows(_env):
    return _env


def _engine(monkeypatch, response, seen=None):
    def dial(request):
        if seen is not None:
            seen.append(request)
        return response

    monkeypatch.setattr(shim, "_dial", dial)


def test_engine_deny_is_rendered_for_codex(monkeypatch, rows):
    seen = []
    _engine(monkeypatch, {"result": {"permissionDecision": "deny", "permissionDecisionReason": "no"}}, seen)
    out = shim.run("codex", json.dumps(CODEX_LS))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen[0]["method"] == "warm_guard.evaluate"
    assert seen[0]["params"]["payload"]["session_id"] == "sess-1"
    assert seen[0]["params"]["payload"]["tool_name"] == "Bash"
    assert rows == []


def test_engine_allow_agy_is_explicit_allow(monkeypatch, rows):
    _engine(monkeypatch, {"result": {}})
    assert shim.run("agy", json.dumps(AGY_LS)) == {"decision": "allow"}
    assert rows == []


def test_engine_allow_not_overridden_by_backstop(monkeypatch, rows):
    _engine(monkeypatch, {"result": {}})
    assert shim.run("codex", json.dumps(CODEX_PUSH)) == {}
    assert shim.run("agy", json.dumps(AGY_PUSH)) == {"decision": "allow"}


def test_session_identity_comes_from_payload(monkeypatch):
    import os

    seen = []
    monkeypatch.setattr(shim, "_dial", lambda request: seen.append(os.environ.get("COORDINATOR_SESSION_ID")))
    shim.run("agy", json.dumps(AGY_LS))
    assert seen == ["agy-1"]
    assert "COORDINATOR_SESSION_ID" not in os.environ


@pytest.mark.parametrize("response", [None, {"error": {"code": -32601}}, {"result": "x"}, "junk"])
def test_engine_down_or_error_is_neutral_fallback_with_degrade_row(monkeypatch, rows, response):
    _engine(monkeypatch, response)
    assert shim.run("codex", json.dumps(CODEX_LS)) == {}
    assert shim.run("agy", json.dumps(AGY_LS))["decision"] == "ask"
    assert [r["kind"] for r in rows] == [telemetry.KIND_COLD_RUN] * 2
    assert all(r["cause"] for r in rows)


@pytest.mark.deliberate_wall_clock(reason="bounds the shim dial budget against a sleeping engine; sleep burns no CPU")
def test_slow_engine_past_budget_falls_back_within_wall(monkeypatch, rows):
    monkeypatch.setattr(shim, "DIAL_BUDGET_SECS", 0.05)
    monkeypatch.setattr(shim, "_dial", lambda request: time.sleep(2) or {"result": {}})
    start = time.monotonic()
    out = shim.run("codex", json.dumps(CODEX_LS))
    assert time.monotonic() - start < 1.0
    assert out == {}
    assert [r["kind"] for r in rows] == [telemetry.KIND_HOOK_TIMEOUT]


def test_engine_down_irreversible_command_denied(monkeypatch, rows):
    _engine(monkeypatch, None)
    out = shim.run("codex", json.dumps(CODEX_PUSH))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert shim.run("agy", json.dumps(AGY_PUSH))["decision"] == "deny"
    assert rows == []


def test_slow_engine_irreversible_command_denied(monkeypatch):
    monkeypatch.setattr(shim, "DIAL_BUDGET_SECS", 0.05)
    monkeypatch.setattr(shim, "_dial", lambda request: time.sleep(2))
    out = shim.run("codex", json.dumps(CODEX_PUSH))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_engine_down_ordinary_command_fails_open(monkeypatch, rows):
    _engine(monkeypatch, None)
    assert shim.run("codex", json.dumps(CODEX_LS)) == {}
    assert len(rows) == 1


def test_unserved_event_is_neutral_without_dialing(monkeypatch, rows):
    def boom(request):
        raise AssertionError("dialed")

    monkeypatch.setattr(shim, "_dial", boom)
    assert shim.run("codex", json.dumps({**CODEX_LS, "hook_event_name": "PostToolUse"})) == {}


def test_dial_exception_is_a_miss(monkeypatch, rows):
    def boom(request):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(shim, "_dial", boom)
    assert shim.run("codex", json.dumps(CODEX_LS)) == {}
    assert len(rows) == 1


def test_garbage_stdin_is_neutral_fallback(monkeypatch):
    _engine(monkeypatch, None)
    assert shim.run("codex", "not json") == {}
    assert shim.run("agy", "not json")["decision"] == "ask"
    assert shim.run("codex", "") == {}
    assert shim.run("nope", "{}") == {}


def test_degrade_raising_still_answers(monkeypatch):
    _engine(monkeypatch, None)

    def boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(telemetry, "record_degrade", boom)
    assert shim.run("codex", json.dumps(CODEX_LS)) == {}


def test_main_prints_json_and_returns_zero(monkeypatch, capsys):
    _engine(monkeypatch, {"result": {}})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(AGY_LS)))
    assert shim.main(["--host", "agy"]) == 0
    assert json.loads(capsys.readouterr().out) == {"decision": "allow"}


def test_main_without_host_exits_zero_with_empty_answer(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert shim.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {}
