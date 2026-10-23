"""Audience pins for the test-suite and raw-pid-liveness guards.

Each case drives the production `check`: the EM render is the legacy text
verbatim, the SUBAGENT render carries an escalation route and no override, and
a non-matching input still allows.
"""
from __future__ import annotations

import pytest

from coordinator_core import _hook_envelope
from coordinator_core.bash_guards import check_raw_pid_liveness as pid_guard
from coordinator_core.bash_guards import check_test_suite_invocation as suite_guard
from coordinator_core.guard_messages.alternative_record import (
    AdaptableOffer,
    AlternativeRecord,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.testing import envelope_of

_AGENT_ID = "a0123456789abcdef"


def _reason(envelope) -> str:
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["coordinator_core", "coordinator/tests"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(suite_guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(suite_guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(suite_guard, "_is_cloud_box", lambda payload: False)
    monkeypatch.setattr(suite_guard, "_tier_u_grant", lambda cwd: (False, None))
    monkeypatch.setattr(suite_guard, "_mutex_holder", lambda: None)
    return tmp_path


def _suite_payload(command, cwd, agent_id=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(cwd),
    }
    if agent_id:
        p["agent_id"] = agent_id
    return p


def test_suite_subagent_deny_is_a_record_with_route_and_no_override(repo):
    result = suite_guard.check(_suite_payload("pytest", repo, _AGENT_ID))
    assert isinstance(result, AlternativeRecord)
    assert result.verdict_kind == "deny"
    assert any(isinstance(m, EscalationRoute) for m in result.members)
    assert not any(isinstance(m, Override) for m in result.members)
    assert "Override" not in _reason(envelope_of(result, "subagent"))


def test_suite_em_grant_deny_carries_override_and_verbatim_em_text(repo):
    result = suite_guard.check(_suite_payload("pytest", repo))
    assert isinstance(result, AlternativeRecord)
    assert any(isinstance(m, Override) for m in result.members)
    assert any(isinstance(m, EscalationRoute) for m in result.members)
    assert _reason(envelope_of(result, "em")) == result.em_text == result.summary


def test_suite_mutex_deny_is_a_record(repo, monkeypatch):
    monkeypatch.setattr(suite_guard, "_tier_u_grant", lambda cwd: (True, None))
    monkeypatch.setattr(
        suite_guard, "_command_wrapped_in_suite_mutex", lambda *a, **k: True
    )
    monkeypatch.setattr(
        suite_guard, "_mutex_holder", lambda: {"pid": 1, "command": "pytest"}
    )
    result = suite_guard.check(_suite_payload("pytest", repo))
    assert isinstance(result, AlternativeRecord)
    assert result.verdict_kind == "deny"
    assert any(isinstance(m, Override) for m in result.members)


def test_suite_scoped_run_for_em_still_allows(repo):
    assert suite_guard.check(_suite_payload("pytest tests/test_x.py", repo)) is None


def _pid_payload(command, agent_id=None):
    p = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/repo"}
    if agent_id:
        p["agent_id"] = agent_id
    return p


@pytest.fixture
def pid_env(monkeypatch):
    monkeypatch.delenv(pid_guard._OVERRIDE_ENV, raising=False)


def test_pid_deny_is_a_record_with_two_offers_and_an_override(pid_env):
    result = pid_guard.check(_pid_payload("ps -p $PID"))
    assert isinstance(result, AlternativeRecord)
    assert sum(isinstance(m, AdaptableOffer) for m in result.members) == 2
    assert any(isinstance(m, Override) for m in result.members)


def test_pid_em_render_is_the_stamped_legacy_reason(pid_env):
    payload = _pid_payload("kill -0 $pid")
    result = pid_guard.check(payload)
    legacy = _hook_envelope._stamp(pid_guard._deny_reason("kill -0", payload=payload))
    assert _reason(envelope_of(result, "em")) == legacy


def test_pid_subagent_render_has_a_route_and_no_override(pid_env):
    result = pid_guard.check(_pid_payload("ps -p $PID", _AGENT_ID))
    assert any(isinstance(m, EscalationRoute) for m in result.members)
    rendered = envelope_of(result, "subagent")
    assert rendered["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pid_unrelated_command_still_allows(pid_env):
    assert pid_guard.check(_pid_payload("ps aux")) is None
