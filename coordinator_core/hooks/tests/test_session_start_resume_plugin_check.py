"""session_start_resume_plugin_check: names the coordinator relaunch for an EM-role session without agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import session_start_resume_plugin_check as check
from coordinator_core.hooks.support import turn_intent
from coordinator_core.ipc import _REGISTRY

SID = "0a1b2c3d-1111-2222-3333-444455556666"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    return root


def _mark_em(root: Path, sid: str = SID) -> None:
    directory = turn_intent.session_dir(str(root / ".git"), sid)
    assert turn_intent.write_role_marker(directory, sid)


def _plugin_with_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "agents").mkdir(parents=True)
    (plugin / "agents" / "executor.md").write_text("---\nname: executor\n---\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))


def test_op_registered() -> None:
    assert "hooks.session_start_resume_plugin_check" in _REGISTRY


def test_em_role_without_agents_names_the_relaunch(repo: Path) -> None:
    _mark_em(repo)
    result = check._handler({"session_id": SID, "cwd": str(repo)})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert f"coordinator --resume {SID}" in context
    assert "permissionDecision" not in result["hookSpecificOutput"]


def test_em_role_with_an_agent_less_plugin_root_still_names_the_relaunch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mark_em(repo)
    (tmp_path / "bare").mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "bare"))
    assert check.build_report({"session_id": SID}) is not None


def test_em_role_with_coordinator_agents_is_silent(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark_em(repo)
    _plugin_with_agents(tmp_path, monkeypatch)
    assert check.build_report({"session_id": SID}) is None
    assert "additionalContext" not in str(check._handler({"session_id": SID}))


def test_non_em_session_without_agents_is_silent(repo: Path) -> None:
    assert check.build_report({"session_id": SID, "cwd": str(repo)}) is None


def test_missing_session_id_is_silent(repo: Path) -> None:
    assert check.build_report({}) is None


def test_outside_a_git_repo_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert check.build_report({"session_id": SID}) is None
