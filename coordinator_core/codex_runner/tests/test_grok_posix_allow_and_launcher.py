"""POSIX grok stage: general stages bypass permissions; invoke-only children carry the absolute launcher in argv and prompt."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from coordinator_core.codex_runner import workflow
from coordinator_core.ops.workflow_fire import worker_preamble

_FLAT = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
_NESTED = '"${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/bin/coordinator-invoke"'


class _PosixOs:
    """os with name forced to posix; pathlib keeps the real os so Path stays constructible on Windows."""

    name = "posix"

    def __getattr__(self, attr: str):
        import os

        return getattr(os, attr)


@pytest.fixture
def posix_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "settings-home"
    for module in (workflow, worker_preamble):
        monkeypatch.setattr(module, "os", _PosixOs())
        monkeypatch.setattr(module, "settings_home", lambda: home)
    monkeypatch.setattr(workflow, "resolve_grok", lambda: ["/bin/grok-x"])
    return home


def _allows(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--allow"]


def _mode(argv: list[str]) -> str:
    return argv[argv.index("--permission-mode") + 1]


def test_general_grok_stage_bypasses_permissions(posix_home: Path, tmp_path: Path) -> None:
    for agent_type in (None, "coordinator:executor"):
        argv = workflow._family_argv("grok", tmp_path, agent_type)
        assert _mode(argv) == "bypassPermissions"
        assert "Bash" in _allows(argv) and workflow._EXAMPLE_RETRIEVAL_REPO_ALLOW in _allows(argv)


@pytest.mark.parametrize("agent_type", sorted(workflow._INVOKE_ONLY_AGENT_TYPES))
def test_invoke_only_child_gets_exactly_the_absolute_launcher_allow(
    posix_home: Path, tmp_path: Path, agent_type: str
) -> None:
    argv = workflow._family_argv("grok", tmp_path, agent_type)
    launcher = (posix_home / "bin" / "coordinator-invoke").as_posix()
    assert _mode(argv) != "bypassPermissions"
    assert _allows(argv) == [f"Bash({launcher}:*)"]


def test_grok_prompt_launcher_matches_the_invoke_only_allow(posix_home: Path, tmp_path: Path) -> None:
    prompt = worker_preamble.with_worker_preamble("grok", f"{_FLAT} dispatch.emit\n{_NESTED} dispatch.ask_stage")
    launcher = (posix_home / "bin" / "coordinator-invoke").as_posix()
    (allow,) = _allows(workflow._family_argv("grok", tmp_path, "coordinator:git-commit-agent"))
    assert allow == f"Bash({launcher}:*)"
    assert prompt.count(launcher) == 2
    assert f"{launcher} dispatch.emit" in prompt and f"{launcher} dispatch.ask_stage" in prompt
    assert "COORDINATOR_SETTINGS_HOME" not in prompt and "CLAUDE_HOME" not in prompt and "${" not in prompt
    assert f'"{launcher}"' not in prompt


def test_bare_name_allow_would_not_match_the_absolute_prompt_launcher(posix_home: Path, tmp_path: Path) -> None:
    prompt = worker_preamble.with_worker_preamble("grok", f"{_FLAT} dispatch.emit")
    assert workflow._RELAY_ALLOW not in _allows(workflow._family_argv("grok", tmp_path, "coordinator:op-relay"))
    assert "Bash(coordinator-invoke:*)" == workflow._RELAY_ALLOW
    assert not any(line.lstrip().startswith("coordinator-invoke ") for line in prompt.splitlines())
