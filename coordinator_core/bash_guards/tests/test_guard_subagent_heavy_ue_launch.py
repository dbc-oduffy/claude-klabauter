"""guard-subagent-heavy-ue-launch: a subagent's UE build/editor launch is denied, a mention never is.

Cases mirror coordinator-content-repo coordinator/tests/test_guard_subagent_heavy_ue_launch.py (169088ceb) so
the cold script and this port judge the same commands alike.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.guard_subagent_heavy_ue_launch import check as _check
from coordinator_core.bash_guards.roster import guard_roster
from coordinator_core.guard_messages.testing import envelope_of


def check(*args, **kwargs):
    return envelope_of(_check(*args, **kwargs))


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


@pytest.mark.parametrize(
    "command",
    [
        "python - <<'EOF'\nUnrealEditor-Cmd p.uproject -run=pythonscript\nEOF",
        "python - <<-'EOF'\n\tUnrealEditor-Cmd p.uproject\n\tEOF\n",
        'python - <<"EOF"\nUnrealEditor-Cmd p.uproject\nEOF\necho ok',
    ],
)
def test_heredoc_data_is_not_a_launch(command):
    assert check(_payload(command)) is None


@pytest.mark.parametrize(
    "command",
    [
        "UnrealEditor-Cmd p.uproject -run=pythonscript <<'EOF'\nx\nEOF",
        "cat <<'EOF'\nsome data\nEOF\nUnrealEditor-Cmd p.uproject",
    ],
)
def test_heredoc_opener_and_later_launch_still_denied(command):
    assert check(_payload(command)) is not None


@pytest.mark.parametrize(
    "command",
    [
        "python -m likeness.dense_map_2d.bfm_operator --in a",
        "python3 -m likeness.dense_map_2d.bfm_operator",
        "py -3 -m likeness.dense_map_2d.bfm_operator",
        "python -u -m likeness.dense_map_2d.bfm_operator",
        "python likeness/dense_map_2d/bfm_operator.py --in a",
        "python3 ./bfm_operator.py",
        "python -W ignore bfm_operator.py",
        "python -X utf8 -m likeness.dense_map_2d.bfm_operator",
        "py bfm_operator.py",
        ".venv\\Scripts\\python.exe -m likeness.dense_map_2d.bfm_operator",
        "cd /x && PYTHONPATH=. python -m likeness.dense_map_2d.bfm_operator",
    ],
)
def test_denies_a_declared_heavy_python_cli(command):
    result = check(_payload(command))
    assert result is not None
    reason = _reason(result)
    assert "needs_slot" in reason
    assert "Unreal" not in reason


def test_every_declared_heavy_python_name_passes_the_prefilter():
    from coordinator_core.bash_guards import guard_subagent_heavy_ue_launch as g

    declared = {m.rsplit(".", 1)[-1] for m in g._HEAVY_PY_MODULES} | set(g._HEAVY_PY_SCRIPTS)
    assert declared
    for name in declared:
        assert any(p in name.lower() for p in g._PY_PREFILTER), name


@pytest.mark.parametrize(
    "command", ["python -m likeness.dense_map_2d.overlay --in a", "python overlay_other.py"]
)
def test_overlay_module_is_denied_only_as_a_declared_module(command):
    assert (check(_payload(command)) is not None) == ("-m" in command)


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/test_x.py",
        "python3 scripts/other.py",
        "py -3 -m json.tool a.json",
        "grep -n bfm_operator foo.py",
        "echo python -m likeness.dense_map_2d.bfm_operator",
        "python tools/x.py bfm_operator",
    ],
)
def test_allows_a_non_declared_python_command(command):
    assert check(_payload(command)) is None


def test_declared_heavy_python_is_not_guarded_for_the_main_loop():
    assert check(_payload("python -m likeness.dense_map_2d.bfm_operator", agent_id=None)) is None


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


OVERLAY_CMD = (
    "cd /fixture/likeness-prototype; python -m likeness.dense_map_2d.overlay "
    "--run-key 2026-10-27-hrn-photo-run --photos-key fifa-photos "
    "--out-key 2026-10-27-dv01-end/png-kane > scratch/dv01-c3-overlay.log 2>&1"
)


def test_the_overlay_cli_with_cd_prefix_and_redirect_launches_heavy():
    from coordinator_core.bash_guards.guard_subagent_heavy_ue_launch import launches_heavy

    assert launches_heavy(OVERLAY_CMD)


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest a.py",
        "python -m json.tool",
        "python -m likeness.dense_map_2d.other",
    ],
)
def test_light_python_modules_do_not_launch_heavy(command):
    from coordinator_core.bash_guards.guard_subagent_heavy_ue_launch import launches_heavy

    assert not launches_heavy(command)


@pytest.mark.parametrize(
    "command",
    [
        "npx electron .",
        "cd app && pnpm dev",
        "pnpm --filter desktop dev",
        "npm run dev",
        "yarn dev:electron",
        "pnpm exec electron .",
        "./node_modules/.bin/electron .",
        "bash -c 'pnpm dev'",
    ],
)
def test_denies_a_subagent_electron_or_dev_server(command):
    reason = _reason(check(_payload(command)))
    assert "scoped_pytest" in reason and "Group EM slot" in reason


def test_denies_a_background_shell_storm_on_windows(monkeypatch):
    monkeypatch.setattr("coordinator_core.bash_guards.guard_subagent_heavy_ue_launch._storm_guard_applies", lambda: True)
    reason = _reason(check(_payload("sleep 1 & sleep 2 & sleep 3 & wait")))
    assert "background-shell fan-out" in reason


@pytest.mark.parametrize(
    "command",
    [
        "for i in 1 2 3; do work $i & done; wait",
        "while read x; do ./job $x & done < list",
        "(for f in a b; do job $f & done)",
    ],
)
def test_denies_a_loop_background_fanout_on_windows(monkeypatch, command):
    monkeypatch.setattr("coordinator_core.bash_guards.guard_subagent_heavy_ue_launch.sys.platform", "win32")
    assert "background-shell fan-out" in _reason(check(_payload(command)))


@pytest.mark.parametrize(
    "command",
    [
        "for i in 1 2 3; do work $i; done",
        "for i in 1 2; do work $i && other; done",
        "for i in 1 2; do echo 'a & b'; done",
        "work & wait",
        "for i in 1 2; do work $i; done; other &",
    ],
)
def test_allows_a_loop_without_background_on_windows(monkeypatch, command):
    monkeypatch.setattr("coordinator_core.bash_guards.guard_subagent_heavy_ue_launch.sys.platform", "win32")
    assert check(_payload(command)) is None


def test_allows_a_loop_background_fanout_on_posix(monkeypatch):
    monkeypatch.setattr("coordinator_core.bash_guards.guard_subagent_heavy_ue_launch.sys.platform", "linux")
    assert check(_payload("for i in 1 2 3; do work $i & done")) is None


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_allows_a_background_shell_storm_on_posix(monkeypatch, platform):
    monkeypatch.setattr("coordinator_core.bash_guards.guard_subagent_heavy_ue_launch.sys.platform", platform)
    assert check(_payload("sleep 1 & sleep 2 & sleep 3 & wait")) is None


@pytest.mark.parametrize(
    "command",
    [
        "npx electron .",
        "pnpm dev",
        "sleep 1 & sleep 2 & sleep 3 & wait",
    ],
)
def test_the_main_loop_may_run_them(command):
    assert check(_payload(command, agent_id=None)) is None


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest coordinator_core/foo/tests/test_a.py",
        "python -m coordinator_core.testing.scoped_pytest --allow x -- a.py",
        "pnpm typecheck",
        "pnpm add electron",
        "echo npx electron",
        "grep -n 'pnpm dev' README.md",
        "make build && echo ok",
        "sleep 1 & sleep 2 & wait",
        "a && b 2>&1 | tee out; c >/dev/null 2>&1",
        "echo 'a & b & c & d'",
    ],
)
def test_allows_scoped_tests_and_mentions(command):
    assert check(_payload(command)) is None
