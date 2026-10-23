"""coordinator_core.bash_guards.tests.test_guard_host_subagent_bash_spawn_shapes_dialect_aware --
behavioural pin for `guard_host_subagent_bash_spawn_shapes.check` across both
shell dialects.

The guard bans a SHAPE, not a tool: a PowerShell fan-out is refused with
PowerShell detection (`classify_command(..., dialect=...)`), not bash-text
detection. The sibling `guard_host_subagent_bash_ban` stays Bash-only on
purpose (PowerShell is its sanctioned alternative) -- pinned here so the two
are never "made consistent".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.bash_guards import guard_host_subagent_bash_ban as ban
from coordinator_core.bash_guards import guard_host_subagent_bash_spawn_shapes as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

_PS_FANOUT = (
    "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'py*' } "
    "| ForEach-Object { Stop-Process $_.Id }"
)
_BASH_FANOUT = "for f in *.py; do echo $f; done"


@pytest.fixture
def opted_in_repo(tmp_path: Path) -> Path:
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_spawn_shapes: deny\n---\n", encoding="utf-8"
    )
    return tmp_path


def _check(cwd: Path, tool_name: str, command: str, agent_id: str = "dispatched-1"):
    return _enveloped(guard.check)(
        {
            "tool_name": tool_name,
            "agent_id": agent_id,
            "tool_input": {"command": command},
            "cwd": str(cwd),
        }
    )


def _reason(result: Dict[str, Any]) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_powershell_fanout_is_denied(opted_in_repo: Path) -> None:
    result = _check(opted_in_repo, "PowerShell", _PS_FANOUT)
    assert result is not None, "PowerShell fan-out was allowed"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "pipeline_foreach_object" in _reason(result)


def test_bash_fanout_is_still_denied(opted_in_repo: Path) -> None:
    result = _check(opted_in_repo, "Bash", _BASH_FANOUT)
    assert result is not None
    assert "for_loop" in _reason(result)


@pytest.mark.parametrize(
    "tool, command",
    [("Bash", "cat one-known-file.txt"), ("PowerShell", "Get-Content one-known-file.txt")],
)
def test_a_single_read_is_allowed_in_either_shell(opted_in_repo: Path, tool: str, command: str) -> None:
    assert _check(opted_in_repo, tool, command) is None


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_the_em_is_never_caught(opted_in_repo: Path, tool: str) -> None:
    command = _PS_FANOUT if tool == "PowerShell" else _BASH_FANOUT
    assert _check(opted_in_repo, tool, command, agent_id="") is None


def test_the_refusal_names_the_cost_the_caller_actually_pays(opted_in_repo: Path) -> None:
    result = _check(opted_in_repo, "PowerShell", _PS_FANOUT)
    assert result is not None
    text = _reason(result)
    assert "bash.exe" not in text
    assert "ForEach-Object" in text
    assert "the PowerShell tool is always available" not in text


def test_the_ban_guard_stays_bash_only_on_purpose(tmp_path: Path) -> None:
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\n---\n", encoding="utf-8"
    )
    payload = {
        "tool_name": "PowerShell",
        "agent_id": "dispatched-1",
        "tool_input": {"command": _PS_FANOUT},
        "cwd": str(tmp_path),
    }
    assert _enveloped(ban.check)(payload) is None
    assert ban.MATCHERS == ("Bash",)


def test_a_tool_with_no_resolvable_dialect_is_allowed_not_denied(opted_in_repo: Path) -> None:
    """The guard sits on the tool path for every dispatched agent: a tool name
    it cannot resolve to a dialect must degrade to allow, never to a deny."""
    assert _check(opted_in_repo, "Read", _BASH_FANOUT) is None


@pytest.mark.parametrize(
    "config",
    [
        "---\nproject_type: general\n\n# Notes\nsubagent_bash_spawn_shapes: deny\n",
        "---\nnested:\n  subagent_bash_spawn_shapes: deny\n---\n",
    ],
    ids=["unterminated_fence", "indented_key"],
)
def test_a_policy_line_that_is_not_frontmatter_declaration_allows(tmp_path: Path, config: str) -> None:
    (tmp_path / "coordinator.local.md").write_text(config, encoding="utf-8")
    assert _check(tmp_path, "Bash", _BASH_FANOUT) is None
