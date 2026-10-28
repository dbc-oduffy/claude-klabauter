"""own_claude_session_token / claude_session_grep_pattern: pure env logic, no git spawn."""

import re

import pytest

from coordinator_core.git import commit_trailers as ct

SID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def cold(monkeypatch):
    monkeypatch.setattr(ct._session_core, "in_warm_served_request", lambda: False)
    monkeypatch.setattr(ct._session_core, "attributable_session_id", lambda cwd=None: SID)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_OWN123")


def test_cold_matching_session_returns_token(cold):
    assert ct.own_claude_session_token(SID) == "session_OWN123"


def test_warm_returns_none(cold, monkeypatch):
    monkeypatch.setattr(ct._session_core, "in_warm_served_request", lambda: True)
    assert ct.own_claude_session_token(SID) is None


def test_other_session_returns_none(cold):
    assert ct.own_claude_session_token("99999999-2222-4333-8444-555555555555") is None


@pytest.mark.parametrize("value", [None, "", "session_OWN123", "cse_", "cse_a-b", "xcse_A"])
def test_bad_remote_env_returns_none(cold, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CLAUDE_CODE_REMOTE_SESSION_ID")
    else:
        monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", value)
    assert ct.own_claude_session_token(SID) is None


def test_non_uuid_session_returns_none(cold, monkeypatch):
    monkeypatch.setattr(ct._session_core, "attributable_session_id", lambda cwd=None: "not-a-uuid")
    assert ct.own_claude_session_token("not-a-uuid") is None


def test_pattern_matches_full_line_and_not_extended_suffix():
    pat = re.compile(ct.claude_session_grep_pattern("session_01AB"), re.M)
    assert pat.search("subj\n\nClaude-Session: https://claude.ai/code/session_01AB\n")
    assert not pat.search("Claude-Session: https://claude.ai/code/session_01ABC\n")
    assert not pat.search("Claude-Session: https://claudeXai/code/session_01AB\n")
    assert ct.claude_session_grep_pattern("session_01AB") == (
        r"^Claude-Session: https://claude\.ai/code/session_01AB$"
    )
