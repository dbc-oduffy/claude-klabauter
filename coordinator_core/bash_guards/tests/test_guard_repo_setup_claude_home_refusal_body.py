"""coordinator_core.bash_guards.tests.test_guard_repo_setup_claude_home_refusal_body --
behavioural pin for `guard_repo_setup_claude_home_refusal.check`: a scaffold
invocation whose root resolves to the Claude home is refused; the same
invocation against another clone is not.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.bash_guards import guard_repo_setup_claude_home_refusal as guard

_SCAFFOLD = "python3 -m coordinator_core.install.scaffold_structure --manifest-root x"


def test_scaffold_from_the_claude_home_is_denied(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude_home = home / ".claude"
    claude_home.mkdir(parents=True)
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    result = guard.check(
        {"tool_name": "Bash", "tool_input": {"command": _SCAFFOLD}, "cwd": str(claude_home), "env": env}
    )
    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "scaffold cannot target ~/.claude" in hso["permissionDecisionReason"]


def test_scaffold_from_another_clone_is_allowed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    clone = tmp_path / "clone"
    clone.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    assert guard.check(
        {"tool_name": "Bash", "tool_input": {"command": _SCAFFOLD}, "cwd": str(clone), "env": env}
    ) is None


def test_an_unrelated_command_from_the_claude_home_is_allowed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude_home = home / ".claude"
    claude_home.mkdir(parents=True)
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    assert guard.check(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(claude_home), "env": env}
    ) is None
