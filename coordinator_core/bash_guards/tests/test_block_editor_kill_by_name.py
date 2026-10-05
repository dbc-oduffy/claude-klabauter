"""block_editor_kill_by_name: editors die only by the PID of their own launch."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_editor_kill_by_name as guard


def _payload(cmd: str, tool: str = "Bash", **extra) -> dict:
    return {"tool_name": tool, "tool_input": {"command": cmd}, **extra}


@pytest.mark.parametrize(
    "cmd",
    [
        "taskkill /F /IM UnrealEditor-Cmd.exe",
        "taskkill /IM UnrealEditor.exe /F",
        "taskkill /F /IM UE5Editor.exe",
        "taskkill /f /im ShaderCompileWorker.exe",
        "Stop-Process -Name UnrealEditor -Force",
        "Stop-Process -Name LiveCodingConsole,UnrealTraceServer",
        "Stop-Process UnrealEditor-Cmd",
        "Get-Process UnrealEditor* | Stop-Process -Force",
        "pkill UnrealEditor",
        "killall -9 UnrealTraceServer",
    ],
)
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_name_based_kills_denied(cmd, tool):
    out = guard.check(_payload(cmd, tool))
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Kill by the PID of your own launch" in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "taskkill /F /PID 12345",
        "Stop-Process -Id 12345 -Force",
        "kill 12345",
        "Get-Process UnrealEditor",
        "taskkill /F /IM notepad.exe",
        'echo "taskkill /IM UnrealEditor.exe"',
    ],
)
def test_pid_and_unrelated_allowed(cmd):
    assert guard.check(_payload(cmd)) is None


def test_denied_for_subagents_too():
    out = guard.check(_payload("taskkill /F /IM UnrealEditor.exe", agent_id="a1"))
    assert out is not None
