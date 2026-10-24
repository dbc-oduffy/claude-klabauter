"""preuse_bash_dispatch: the pre-fire survey deny leg's precedence against the chain and the advisory."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch as chain
from coordinator_core.hooks import guard_em_prefire_survey as guard
from coordinator_core.hooks import nudge_em_turn_budget as nudge
from coordinator_core.hooks import preuse_bash_dispatch as mod

_TURN = "[em-turn-budget] test signal"
_GUARD_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": "[em-prefire-survey] guard"}}
_CHAIN_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": "chain"}}
_CHAIN_ADV = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "chain"}}


def _run() -> dict:
    return mod._handler({"payload": {"tool_name": "Bash", "tool_input": {"command": "ls"},
                                     "session_id": "s", "cwd": "."}})


@pytest.fixture(autouse=True)
def _turn(monkeypatch):
    monkeypatch.setattr(nudge, "advisory_text", lambda params: _TURN)


def test_guard_deny_wins_over_turn_advisory(monkeypatch):
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: None)
    monkeypatch.setattr(guard, "evaluate", lambda params: _GUARD_DENY)
    assert _run() == _GUARD_DENY


def test_guard_deny_wins_over_chain_non_deny(monkeypatch):
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: _CHAIN_ADV)
    monkeypatch.setattr(guard, "evaluate", lambda params: _GUARD_DENY)
    assert _run() == _GUARD_DENY


def test_chain_deny_wins_over_guard(monkeypatch):
    seen: list = []
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: _CHAIN_DENY)
    monkeypatch.setattr(guard, "evaluate", lambda params: seen.append(params) or _GUARD_DENY)
    assert _run() == _CHAIN_DENY
    assert seen == []


def test_guard_none_leaves_chain_output_identical(monkeypatch):
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: _CHAIN_ADV)
    monkeypatch.setattr(guard, "evaluate", lambda params: None)
    assert _run() == _CHAIN_ADV


def test_guard_none_leaves_turn_advisory(monkeypatch):
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: None)
    monkeypatch.setattr(guard, "evaluate", lambda params: None)
    assert _TURN in _run()["hookSpecificOutput"]["additionalContext"]


def test_guard_exception_fails_open(monkeypatch):
    def boom(params):
        raise RuntimeError("x")

    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: None)
    monkeypatch.setattr(guard, "evaluate", boom)
    assert _TURN in _run()["hookSpecificOutput"]["additionalContext"]
