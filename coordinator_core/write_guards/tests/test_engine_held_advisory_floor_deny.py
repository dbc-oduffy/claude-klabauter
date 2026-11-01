"""A demoted hard-deny advisory must not shadow an advisory-class guard's deny envelope."""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import engine

_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                "permissionDecisionReason": "floor"}}
_ADVISORY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "advisory"}}
_PAYLOAD = {"tool_name": "Edit", "session_id": "", "tool_input": {"file_path": "x"}}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(engine, "_record_advisory_fire", lambda *a, **k: None)
    # The hard guard's deny is demoted to an advisory envelope (held).
    monkeypatch.setattr(engine, "_apply_level", lambda name, out, sid, cwd: dict(_ADVISORY))


def _guards(monkeypatch, *guards):
    monkeypatch.setattr(engine, "_discover_guards", lambda: (list(guards), []))


def _hard():
    return engine._Guard("hard", "hard-deny", ["Edit"], 45, lambda p: dict(_DENY))


def _adv(env, name="adv"):
    return engine._Guard(name, "advisory", ["Edit"], 127, lambda p: None if env is None else dict(env))


def test_advisory_deny_beats_held_demoted_advisory(monkeypatch):
    _guards(monkeypatch, _hard(), _adv(_DENY))
    assert engine.evaluate(dict(_PAYLOAD)) == _DENY


def test_held_advisory_still_returned_when_no_advisory_denies(monkeypatch):
    _guards(monkeypatch, _hard(), _adv(_ADVISORY), _adv(None, "quiet"))
    assert engine.evaluate(dict(_PAYLOAD)) == _ADVISORY


def test_held_advisory_returned_with_no_advisory_guards(monkeypatch):
    _guards(monkeypatch, _hard())
    assert engine.evaluate(dict(_PAYLOAD)) == _ADVISORY


def test_advisory_deny_beats_held_even_after_non_denying_advisory(monkeypatch):
    first = engine._Guard("first", "advisory", ["Edit"], 10, lambda p: dict(_ADVISORY))
    _guards(monkeypatch, _hard(), first, _adv(_DENY))
    assert engine.evaluate(dict(_PAYLOAD)) == _DENY
