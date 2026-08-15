"""
coordinator_core.pickup_assemble.tests.test_holder_goal_state

Purpose: pins `holder_evidence`'s `holder_goal_state` disambiguation
(2026-08-13, state/handoffs/2026-08-13-session-goal-field-has-no-writer.md
AC3) — the sibling key that distinguishes "no goal declared" (a genuine
empty `goal` on a readable meta.json) from "unreadable" (no holder_sid, no
session dir, or the fail-soft exception path), so a null/empty `holder_goal`
no longer reads ambiguously as either.

Run (from the repo root): python -m pytest
coordinator_core/pickup_assemble/tests/test_holder_goal_state.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.pickup_assemble.holder_evidence import holder_evidence

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_session(repo: Path, sid: str, goal: str) -> Path:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "session_id": sid,
        "branch": "work/test/2026-01-01",
        "pid": "1",
        "last_activity": "2026-01-01T00:00:00Z",
        "goal": goal,
        "stable_pid_capture": "",
    }
    (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return sdir


class TestHolderGoalState:
    def test_no_holder_sid_is_unreadable(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        result = holder_evidence(None, repo)
        assert result["holder_goal_state"] == "unreadable"
        assert result["holder_goal"] is None

    def test_missing_session_dir_is_unreadable(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        result = holder_evidence("no-such-session", repo)
        assert result["holder_goal_state"] == "unreadable"
        assert result["holder_goal"] is None

    def test_declared_goal(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_session(repo, "sess-declared", "pickup: Some Title")
        result = holder_evidence("sess-declared", repo)
        assert result["holder_goal_state"] == "declared"
        assert result["holder_goal"] == "pickup: Some Title"

    def test_undeclared_goal(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_session(repo, "sess-undeclared", "")
        result = holder_evidence("sess-undeclared", repo)
        assert result["holder_goal_state"] == "undeclared"
        assert result["holder_goal"] is None

    def test_exception_path_is_unreadable(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_session(repo, "sess-boom", "pickup: whatever")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated liveness_basis failure")

        monkeypatch.setattr(
            "coordinator_core.pickup_assemble.holder_evidence._liveness_basis", _boom
        )
        result = holder_evidence("sess-boom", repo)
        assert result["holder_goal_state"] == "unreadable"
        assert result["holder_goal"] is None
        assert "evidence_error" in result

    def test_exception_after_goal_resolved_preserves_declared_goal(
        self, tmp_path, monkeypatch
    ):
        """Review: code-reviewer P3 (2026-08-13) — an exception in the
        transcript/recent-paths block (which runs after holder_goal is
        already resolved) must not discard a genuinely `declared` goal
        down to `unreadable`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_session(repo, "sess-late-boom", "pickup: already read")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated transcript-resolution failure")

        monkeypatch.setattr(
            "coordinator_core.pickup_assemble.holder_evidence._resolve_transcript",
            _boom,
        )
        result = holder_evidence("sess-late-boom", repo, want_activity=True)
        assert result["holder_goal"] == "pickup: already read"
        assert result["holder_goal_state"] == "declared"
        assert result["holder_branch"] == "work/test/2026-01-01"
        assert "evidence_error" in result
