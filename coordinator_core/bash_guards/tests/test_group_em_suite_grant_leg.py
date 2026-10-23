"""The Bash suite guard honours a live Group EM test-run grant and denies a subagent issuing one."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.group_em import suite_grant
from coordinator_core.session import grant
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

_FULL = "python -m pytest"
_ISSUE = 'tier-u-grant-cli group-em-grant x "y"'
_AGENT = "a0123456789abcdef"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"coordinator_core\"]\n", encoding="utf-8"
    )
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    monkeypatch.setattr(grant, "check_tier_u_grant", lambda cwd=None, **k: (False, None))
    return tmp_path


def _payload(cmd, repo, agent_id=None):
    p = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s", "cwd": str(repo), "env": {}}
    if agent_id:
        p["agent_id"] = agent_id
    return p


def _reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_live_group_em_grant_clears_the_authority_leg(repo, monkeypatch):
    monkeypatch.setattr(suite_grant, "check_group_em_suite_grant", lambda *a, **k: (True, {"k": 1}))
    assert guard._tier_u_grant(str(repo)) == (True, {"k": 1})
    assert "with-suite-mutex" in _reason(_enveloped(guard.check)(_payload(_FULL, repo)))


def test_expired_group_em_grant_is_denied_with_both_remedy_lines(repo, monkeypatch):
    monkeypatch.setattr(suite_grant, "check_group_em_suite_grant", lambda *a, **k: (False, {"k": 1}))
    reason = _reason(_enveloped(guard.check)(_payload(_FULL, repo)))
    assert "with-suite-mutex" not in reason
    assert "tier-u-grant-cli grant pm" in reason
    assert 'tier-u-grant-cli group-em-grant <session> "<ruling>"' in reason


def test_pm_grant_record_is_returned_when_both_legs_miss(repo, monkeypatch):
    rec = {"session_id": "z", "granted_by": "pm"}
    monkeypatch.setattr(grant, "check_tier_u_grant", lambda cwd=None, **k: (False, rec))
    monkeypatch.setattr(suite_grant, "check_group_em_suite_grant", lambda *a, **k: (False, {"k": 1}))
    assert guard._tier_u_grant(str(repo)) == (False, rec)


def test_group_em_reader_error_reads_ungranted(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(suite_grant, "check_group_em_suite_grant", boom)
    assert guard._tier_u_grant(str(repo)) == (False, None)


def test_subagent_issuing_group_em_grant_is_denied(repo):
    out = _enveloped(guard.check)(_payload(_ISSUE, repo, agent_id=_AGENT))
    assert out is not None
    assert "Group EM issues this grant itself" in _reason(out)


@pytest.mark.parametrize(
    "cmd",
    [
        "python coordinator/bin/tier-u-grant-cli.py group-em-grant x y",
        "tier-u-grant-cli.cmd group-em-grant x y",
    ],
)
def test_subagent_issue_denied_through_interpreter_and_suffix(repo, cmd):
    assert _enveloped(guard.check)(_payload(cmd, repo, agent_id=_AGENT)) is not None


def test_em_issuing_group_em_grant_is_not_denied_by_this_leg(repo):
    assert _enveloped(guard.check)(_payload(_ISSUE, repo)) is None


def test_subagent_other_grant_verbs_are_not_denied_by_this_leg(repo):
    assert _enveloped(guard.check)(_payload('tier-u-grant-cli read', repo, agent_id=_AGENT)) is None
