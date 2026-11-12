"""An EM Edit/Write to a CLAUDE.md-class or rules file is denied naming the doctrine-edit sentinel, and admitted when a fresh one is present."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.write_guards import _doctrine_edit_sentinel as sentinel
from coordinator_core.write_guards import guard_doctrine_surface_edits as guard

_SUBAGENT = {"agent_id": "run-1", "agent_type": "executor"}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "rules").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("# doctrine\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo))
    monkeypatch.setattr(guard, "read_content_root", lambda: None)
    monkeypatch.setattr(sentinel, "read_content_root", lambda: None)
    monkeypatch.setattr(guard, "gem_admitted", lambda *a, **k: False)
    monkeypatch.setattr(guard, "feature_enabled", lambda name: True)
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "MATCH", "message": ""}
    )
    monkeypatch.setattr(guard, "_write_repo_identity_advisory_log", lambda *a, **k: None)
    guard._content_root_memo.clear()
    yield repo, home
    guard._content_root_memo.clear()


def _edit(target, repo, **extra):
    return {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "cwd": str(repo),
        "tool_input": {"file_path": str(target), "old_string": "doctrine", "new_string": "rule"},
        **extra,
    }


def _touch_sentinel(repo, age=0):
    path = repo / ".git" / sentinel.SENTINEL_BASENAME
    path.write_text("", encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def _targets(repo, home):
    return {"claude_md": repo / "CLAUDE.md", "rules": home / ".claude" / "rules" / "x.md"}


@pytest.mark.parametrize("kind", ["claude_md", "rules"])
def test_em_edit_without_sentinel_is_denied_naming_the_route(box, kind):
    repo, home = box
    target = _targets(repo, home)[kind]
    target.write_text("# doctrine\n", encoding="utf-8")
    out = guard.check(_edit(target, repo))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert sentinel.DENY_REASON in out["permissionDecisionReason"]
    assert ".coordinator-doctrine-edit-approved" in out["permissionDecisionReason"]


@pytest.mark.parametrize("kind", ["claude_md", "rules"])
def test_em_edit_with_fresh_sentinel_is_admitted(box, kind):
    repo, home = box
    target = _targets(repo, home)[kind]
    target.write_text("# doctrine\n", encoding="utf-8")
    _touch_sentinel(repo)
    out = guard.check(_edit(target, repo))["hookSpecificOutput"]
    assert "permissionDecision" not in out


@pytest.mark.parametrize("kind", ["claude_md", "rules"])
def test_stale_sentinel_does_not_admit(box, kind):
    repo, home = box
    target = _targets(repo, home)[kind]
    target.write_text("# doctrine\n", encoding="utf-8")
    _touch_sentinel(repo, age=sentinel.FRESH_SECONDS + 60)
    out = guard.check(_edit(target, repo))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_write_creating_a_new_rules_file_is_denied(box):
    repo, home = box
    target = home / ".claude" / "rules" / "new.md"
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "cwd": str(repo),
        "tool_input": {"file_path": str(target), "content": "rule\n"},
    }
    assert guard.check(payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_first_time_create_of_missing_claude_md_stays_admitted(box):
    repo, _home = box
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "cwd": str(repo),
        "tool_input": {"file_path": str(repo / "CLAUDE.md"), "content": "x"},
    }
    (repo / "CLAUDE.md").unlink()
    assert guard.check(payload) is None


def test_subagent_claude_md_edit_is_denied_even_with_the_sentinel(box):
    repo, _home = box
    _touch_sentinel(repo)
    out = guard.check(_edit(repo / "CLAUDE.md", repo, **_SUBAGENT))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "dispatched agent may not edit CLAUDE.md" in out["permissionDecisionReason"]


def test_a_file_outside_every_protected_surface_is_untouched(box):
    repo, home = box
    other = home / ".claude" / "notes.md"
    other.write_text("x", encoding="utf-8")
    assert guard.check(_edit(other, repo)) is None
