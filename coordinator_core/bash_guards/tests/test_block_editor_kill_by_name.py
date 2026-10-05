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
        "taskkill //F //IM UnrealEditor-Cmd.exe //T",
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


@pytest.mark.parametrize(
    ("name", "body", "launch"),
    [
        ("watch4.sh", "while true; do\n  taskkill //F //IM UnrealEditor-Cmd.exe //T\n  sleep 5\ndone\n", "bash {p}"),
        ("w.ps1", "Stop-Process -Name UnrealEditor -Force\n", "pwsh -File {p}"),
        ("w.sh", "pkill -f ShaderCompileWorker\n", "nohup sh {p} &"),
    ],
)
def test_name_kill_inside_a_launched_script_denied(tmp_path, name, body, launch):
    script = tmp_path / name
    script.write_text(body, encoding="utf-8", newline="\n")
    out = guard.check(_payload(launch.format(p=script.as_posix())))
    assert out is not None


def test_relative_script_resolves_against_cwd(tmp_path):
    (tmp_path / "w.sh").write_text("taskkill /F /IM UnrealEditor.exe\n", encoding="utf-8")
    assert guard.check(_payload("bash w.sh", cwd=str(tmp_path))) is not None


def test_script_with_pid_kill_allowed(tmp_path):
    script = tmp_path / "w.sh"
    script.write_text("taskkill /F /PID $(cat editor.pid)\n", encoding="utf-8")
    assert guard.check(_payload(f"bash {script.as_posix()}")) is None


def test_missing_script_allowed():
    assert guard.check(_payload("bash /nonexistent/w.sh")) is None
