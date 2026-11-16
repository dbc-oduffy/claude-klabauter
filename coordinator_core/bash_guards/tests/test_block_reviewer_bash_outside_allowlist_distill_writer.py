"""coordinator:distill-writer Bash is confined to ``coordinator-doc-new --type decision``
through the reviewer Bash guard's guard-local leg; its Write/Edit stay allowed."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)
from coordinator_core.bash_guards import _helpers
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.session import core as session_core
from coordinator_core.write_guards import block_confined_agent_write as write_guard
from pathlib import Path

pytestmark = pytest.mark.complete

_WRITER = "coordinator:distill-writer"
_REVIEWER = "coordinator:code-reviewer"
_DOC_NEW = "/x/coordinator/bin/coordinator-doc-new.py"


@pytest.fixture(autouse=True)
def _stub_roster_absence_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda effective_type: False)


def _confine(monkeypatch, subagent_type) -> None:
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id, **kw: subagent_type
    )


def _decide(monkeypatch, command, subagent_type=_WRITER):
    """Run the guard's default entry point; return the deny envelope or None."""
    _confine(monkeypatch, subagent_type)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": "deadbeef0123",
        "agent_type": subagent_type,
    }
    result = guard.check(payload)
    return None if result is None else envelope_of(result)


@pytest.mark.parametrize(
    "command",
    [
        "coordinator-doc-new --type decision --title x",
        f"{_DOC_NEW} --type decision --title x",
        f"python3 {_DOC_NEW} --type decision --title x",
        "coordinator-doc-new --type=decision --title x",
        "coordinator-doc-new --title x --type decision",
        "coordinator-doc-new.cmd --type decision",
    ],
)
def test_scaffolder_decision_is_allowed(monkeypatch: pytest.MonkeyPatch, command) -> None:
    assert _decide(monkeypatch, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "git status",
        "git show HEAD",
        "cat x",
        "grep -n a b",
        "machine-local get repos.x",
        "coordinator-invoke review.prep",
        "coordinator-doc-new --type review-findings",
        "coordinator-doc-new --type review-probe",
        "coordinator-doc-new --type decisions",
        "coordinator-doc-new --type decision --type review-findings",
        "coordinator-doc-new --type review-findings --type decision",
        "coordinator-doc-new --title x",
        "coordinator-doc-new --type decision | wc -c",
        "ls | wc -l",
        "coordinator-doc-new --type decision; rm -rf x",
        "coordinator-doc-new --type decision && ls",
        "coordinator-doc-new --type decision > out.md",
        "python3 -m pytest",
        "python3 -c 'print(1)'",
        "python3 evil.py --type decision",
        "rm -rf x",
        "",
    ],
)
def test_everything_else_is_denied(monkeypatch: pytest.MonkeyPatch, command) -> None:
    assert _decide(monkeypatch, command) is not None


def test_code_reviewer_ruleset_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _decide(monkeypatch, "git status", _REVIEWER) is None
    assert _decide(monkeypatch, "coordinator-doc-new --type review-findings", _REVIEWER) is None
    assert _decide(monkeypatch, "coordinator-doc-new --type decision", _REVIEWER) is not None


def test_shared_findings_set_does_not_gain_the_writer() -> None:
    assert not _helpers.is_confined_findings_agent(_WRITER)
    assert _WRITER not in session_core._CONFINED_FINDINGS_AGENTS


def test_override_key_is_a_confined_type() -> None:
    policy = guard.load_policy(None)
    for key in guard._DEFAULT_RULESET_TYPE_OVERRIDES:
        assert guard._is_confined_type(key, policy)


def test_leg_removal_unconfines_the_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_SCAFFOLDER_ONLY_TYPES", frozenset())
    assert _decide(monkeypatch, "ls") is None


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_writer_write_and_edit_are_not_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_name) -> None:
    monkeypatch.delenv("COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE", raising=False)
    monkeypatch.setattr(write_guard, "resolve_repo_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(
        write_guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, expected_em_session_id="": _WRITER,
    )
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": str(tmp_path / "docs" / "wiki" / "staged.md")},
        "cwd": str(tmp_path),
        "agent_id": "deadbeef0123",
        "session_id": "sess-12345678",
    }
    assert write_guard.check(payload) is None
