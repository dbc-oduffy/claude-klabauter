"""preuse_bash_dispatch: the turn-budget leg runs only when the guard chain answers nothing."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch as chain
from coordinator_core.hooks import nudge_em_turn_budget as nudge
from coordinator_core.hooks import preuse_bash_dispatch as mod

_TURN = "[em-turn-budget] test signal"
_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                "permissionDecisionReason": "no"}}


def _run() -> dict:
    return mod._handler({"payload": {"tool_name": "Bash", "tool_input": {"command": "ls"},
                                     "session_id": "s", "cwd": "."}})


@pytest.fixture
def calls(monkeypatch):
    seen: list = []

    def fake(params):
        seen.append(params)
        return _TURN

    monkeypatch.setattr(nudge, "advisory_text", fake)
    return seen


def test_chain_deny_returned_unchanged_and_leg_not_called(monkeypatch, calls):
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: _DENY)
    assert _run() == _DENY
    assert calls == []


def test_chain_advisory_wins_and_leg_not_called(monkeypatch, calls):
    adv = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "chain"}}
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: adv)
    assert _run() == adv
    assert calls == []


@pytest.mark.parametrize("verdict", [None, "not-a-dict"])
def test_turn_text_emitted_when_chain_answers_nothing(monkeypatch, calls, verdict):
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: verdict)
    out = _run()
    assert _TURN in out["hookSpecificOutput"]["additionalContext"]
    assert len(calls) == 1


def test_empty_turn_text_is_silent(monkeypatch):
    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: None)
    monkeypatch.setattr(nudge, "advisory_text", lambda params: "")
    assert _run() == {}


def test_leg_exception_is_no_advisory(monkeypatch):
    def boom(params):
        raise RuntimeError("x")

    monkeypatch.setattr(chain, "evaluate_payload_json", lambda *a, **k: None)
    monkeypatch.setattr(nudge, "advisory_text", boom)
    assert _run() == {}
