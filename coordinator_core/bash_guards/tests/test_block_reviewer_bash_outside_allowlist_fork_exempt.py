"""An EM fork is never confined by the reviewer Bash allowlist, even when the
roster is unreadable; reviewer types stay confined under the same condition."""

from __future__ import annotations

from coordinator_core.bash_guards import _helpers
from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)
from coordinator_core.guard_messages.testing import envelope_of

_REVIEWER = "coordinator:code-reviewer"
_COMMANDS = ["python3 -m pytest x.py", "git checkout main", "git add -A", "node --check a.js"]


def _payload(command, agent_type, tool_name="Bash"):
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": "deadbeef0123",
        "agent_type": agent_type,
    }


def _setup(monkeypatch, subagent_type):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, s: "deadbeef0123")
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda g, a, **kw: subagent_type
    )
    monkeypatch.setattr(
        _helpers, "resolve_roster", lambda: (None, "roster unreadable: conflict markers")
    )


def test_fork_unconfined_when_roster_unreadable(monkeypatch):
    _setup(monkeypatch, "fork")
    for cmd in _COMMANDS:
        assert guard.check(_payload(cmd, "fork")) is None


def test_fork_unconfined_on_powershell_when_roster_unreadable(monkeypatch):
    _setup(monkeypatch, "fork")
    assert guard.check(_payload("Remove-Item x", "fork", tool_name="PowerShell")) is None


def test_fork_by_agent_type_alone_unconfined(monkeypatch):
    _setup(monkeypatch, "")
    assert guard.check(_payload("git add -A", "fork")) is None


def test_main_session_without_agent_id_unconfined(monkeypatch):
    _setup(monkeypatch, "")
    payload = _payload("git add -A", "")
    del payload["agent_id"]
    assert guard.check(payload) is None


def test_reviewer_still_confined_when_roster_unreadable(monkeypatch):
    _setup(monkeypatch, _REVIEWER)
    for cmd in _COMMANDS[1:]:
        result = envelope_of(guard.check(_payload(cmd, _REVIEWER)))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_unknown_type_still_confined_when_roster_unreadable(monkeypatch):
    _setup(monkeypatch, "")
    result = envelope_of(guard.check(_payload("git add -A", "invented:type")))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_reviewer_cannot_smuggle_fork_name(monkeypatch):
    _setup(monkeypatch, _REVIEWER)
    result = envelope_of(guard.check(_payload("git add -A", "fork")))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
