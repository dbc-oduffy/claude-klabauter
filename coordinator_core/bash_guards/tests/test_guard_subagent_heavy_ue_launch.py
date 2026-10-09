"""guard-subagent-heavy-ue-launch: a subagent's UE build/editor launch is denied, a mention never is.

Cases mirror coordinator-content-repo coordinator/tests/test_guard_subagent_heavy_ue_launch.py (169088ceb) so
the cold script and this port judge the same commands alike.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.guard_subagent_heavy_ue_launch import check
from coordinator_core.bash_guards.roster import guard_roster


def _payload(command, *, tool="Bash", agent_id="agent-abc123"):
    body = {"tool_name": tool, "tool_input": {"command": command}}
    if agent_id is not None:
        body["agent_id"] = agent_id
    return body


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "tool,command",
    [
        ("PowerShell", '& "..\\UE\\Engine\\Build\\BatchFiles\\Build.bat" Foo Win64 Development'),
        ("PowerShell", 'dotnet "..\\UnrealBuildTool.dll" Foo Win64 Development'),
        ("PowerShell", "pwsh -File scripts/build-plugin.ps1 -EngineVersion 5.8"),
        ("PowerShell", "pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/build-plugin.ps1"),
        ("Bash", "UnrealEditor-Cmd.exe proj.uproject -run=Foo"),
        ("Bash", "cd /x && ./Engine/Build/BatchFiles/RunUAT.sh BuildCookRun"),
        ("Bash", 'cmd /c "..\\UE\\RunUAT.bat BuildGraph"'),
        ("PowerShell", "Start-Process -FilePath ..\\UE\\Binaries\\Win64\\UnrealEditor.exe"),
        ("Bash", "bash scripts/build-plugin.ps1; echo done"),
    ],
)
def test_denies_a_subagent_launch(tool, command):
    result = check(_payload(command, tool=tool))
    assert result is not None
    reason = _reason(result)
    assert "needs_slot" in reason
    assert "AN-EXECUTOR-NEVER-FANS-OUT-HEAVY-RUNS" in reason


@pytest.mark.parametrize(
    "command",
    [
        "grep -n UnrealBuildTool foo.txt",
        "git log -- scripts/build-plugin.ps1",
        "cat Engine/Build/BatchFiles/Build.bat",
        "echo run Build.bat later",
        "Select-String -Pattern UnrealEditor-Cmd log.txt",
        "ls # Build.bat",
        "rg RunUAT.bat | head",
    ],
)
def test_allows_a_mention(command):
    assert check(_payload(command)) is None


@pytest.mark.parametrize("agent_id", [None, "", "   "])
def test_main_loop_is_never_guarded(agent_id):
    assert check(_payload("Build.bat Foo Win64", agent_id=agent_id)) is None


@pytest.mark.parametrize("payload", [None, "not json", [], {"tool_name": "Bash"}])
def test_malformed_payload_allows(payload):
    assert check(payload) is None


def test_non_shell_tool_allows():
    assert check(_payload("Build.bat", tool="Read")) is None


def test_a_warn_box_still_denies_the_subagent(monkeypatch):
    from coordinator_core import machine_profile
    from coordinator_core.bash_guards import dispatch

    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "warn")
    deny = check(_payload("Build.bat Foo Win64"))
    out = dispatch._apply_guard_level("guard-subagent-heavy-ue-launch", deny, subagent=True)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_registered_in_the_roster_as_a_confinement_deny():
    (entry,) = [e for e in guard_roster() if e.id == "guard-subagent-heavy-ue-launch"]
    assert entry.band == "confinement-deny" and entry.fail_closed
    assert entry.script == "bash_guards/guard_subagent_heavy_ue_launch.py"
