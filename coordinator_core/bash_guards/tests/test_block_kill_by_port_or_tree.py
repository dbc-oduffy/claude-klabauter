"""block_kill_by_port_or_tree: deny taskkill /T and port- or name-selected kills, allow spawn-recorded pids.

In-process (no spawn); scripts are files under tmp_path.
"""

from __future__ import annotations

import os
import statistics
import time

import pytest

from coordinator_core.bash_guards import block_kill_by_port_or_tree as guard
from coordinator_core.guard_messages.testing import envelope_of


def denied(command, cwd, tool="Bash", **extra):
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd), **extra}
    out = envelope_of(guard.check(payload))
    if out is None:
        return None
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    reason = spec["permissionDecisionReason"]
    assert guard.ANCHOR in reason
    assert guard.OVERRIDE_ENV not in reason
    return reason


@pytest.mark.parametrize("command,tool", [
    ("taskkill /PID 4242 /T /F", "Bash"),
    ("taskkill //PID 4242 //T //F", "Bash"),
    ("taskkill /T /PID 4242", "PowerShell"),
    ('cmd /c "taskkill /PID 4242 /T /F"', "Bash"),
    ('powershell -Command "taskkill /PID 4242 /F /T"', "Bash"),
    ("taskkill /IM node.exe /F", "Bash"),
    ('taskkill /FI "IMAGENAME eq node.exe"', "PowerShell"),
    ("pkill -f derive", "Bash"),
    ("killall node", "Bash"),
    ("sudo fuser -k 3000/tcp", "Bash"),
    ("Stop-Process -Name node -Force", "PowerShell"),
    ("Stop-Process -Name 'chrome*'", "PowerShell"),
    ("kill -9 $(lsof -ti :3000)", "Bash"),
    ("kill $(pgrep -f derive)", "Bash"),
    ("lsof -ti :3000 | xargs kill -9", "Bash"),
    ("pidof node | xargs -r kill", "Bash"),
    ("netstat -ano | findstr :8080 | ForEach-Object { taskkill /PID $_.Split()[-1] /F }", "PowerShell"),
    ("$p = (Get-NetTCPConnection -LocalPort 8080).OwningProcess; Stop-Process -Id $p -Force", "PowerShell"),
    ("Get-Process -Name node | Stop-Process", "PowerShell"),
    ("Get-Process node | Stop-Process -Force", "PowerShell"),
    ("tasklist | findstr node; taskkill /PID 99 /F", "Bash"),
])
def test_inline_denies(tmp_path, command, tool):
    assert denied(command, tmp_path, tool)


@pytest.mark.parametrize("command,tool", [
    ("sleep 100 & kill $!", "Bash"),
    ("kill $pid", "Bash"),
    ("kill -9 1234", "Bash"),
    ("$proc = Start-Process python -ArgumentList x.py -PassThru; Stop-Process -Id $proc.Id", "PowerShell"),
    ("Stop-Process -Id $proc.Id -Force", "PowerShell"),
    ("taskkill /PID 4242 /F", "Bash"),
    ("taskkill /PID $pid /F", "PowerShell"),
    ("Get-Process -Id $proc.Id | Stop-Process", "PowerShell"),
    ("git commit -m 'guard taskkill /T and pkill by port'", "Bash"),
    ("rg -n kill coordinator/hooks", "Bash"),
    ("python -m pytest coordinator/hooks/tests/test_guard_kill_by_port_or_tree.py", "Bash"),
    ("git add docs/plans/guard-kill-by-port-or-tree.md", "Bash"),
    ("ls -la", "Bash"),
    ("git status", "Bash"),
])
def test_allowed_forms(tmp_path, command, tool):
    assert denied(command, tmp_path, tool) is None


def _write(tmp_path, name, body, age_s=0):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("name,body,command,tool", [
    ("reap.sh", "#!/bin/bash\nPID=$(lsof -ti :3000)\nkill -9 $PID\n", "bash reap.sh", "Bash"),
    ("reap.sh", "pkill -f derive\n", "./reap.sh", "Bash"),
    ("reap.ps1", "$c = Get-NetTCPConnection -LocalPort 3000\nStop-Process -Id $c.OwningProcess -Force\n",
     "pwsh -File reap.ps1", "PowerShell"),
    ("reap.ps1", "taskkill /PID 4242 /T /F\n", "& ./reap.ps1", "PowerShell"),
    ("reap.ps1", "Stop-Process -Name node\n", "powershell.exe -NoProfile -ExecutionPolicy Bypass -File reap.ps1", "PowerShell"),
    ("reap.py", "import subprocess\nsubprocess.run(['taskkill', '/PID', '9', '/T', '/F'])\n",
     "python reap.py", "Bash"),
    ("reap.py", "import psutil\nfor p in psutil.process_iter():\n    p.kill()\n", "python3 reap.py", "Bash"),
    ("reap.py", "import os\nos.system('pkill -f derive')\n", "python reap.py", "Bash"),
])
def test_script_denies(tmp_path, name, body, command, tool):
    _write(tmp_path, name, body)
    reason = denied(command, tmp_path, tool)
    assert reason and f"in script {name}" in reason


def test_script_allowed_forms(tmp_path):
    _write(tmp_path, "stop.sh", "sleep 100 &\nkill $!\n")
    _write(tmp_path, "stop.ps1", "$proc = Start-Process python -PassThru\nStop-Process -Id $proc.Id\n")
    _write(tmp_path, "stop.py", "import subprocess\np = subprocess.Popen(['sleep','9'])\np.kill()\n")
    assert denied("bash stop.sh", tmp_path) is None
    assert denied("pwsh -File stop.ps1", tmp_path, "PowerShell") is None
    assert denied("python stop.py", tmp_path) is None


def test_a_kill_named_only_in_a_script_comment_is_allowed(tmp_path):
    body = (
        "<#\n.SYNOPSIS\n  Kills nothing (Stop-Process, taskkill /F) by itself.\n#>\n"
        "# a caller-side tree kill (taskkill /T) while this script runs reaches it\n"
        "    # indented: pkill -f derive\n"
        "Start-Process pwsh -ArgumentList '-File','leg.ps1'\n"
    )
    _write(tmp_path, "leg-drive.ps1", body)
    _write(tmp_path, "leg.sh", "# reap with pkill -f derive (never)\nsleep 1 &\nkill $!\n")
    assert denied("pwsh -File leg-drive.ps1", tmp_path, "PowerShell") is None
    assert denied("bash leg.sh", tmp_path) is None


def test_a_heredoc_message_beside_a_py_pathspec_is_not_run(tmp_path):
    msg = "git commit -F - -- guard.py <<'EOF'\nfix: a (task" + "kill /T) comment no longer denies\nEOF\n"
    assert denied(msg, tmp_path) is None
    run = "python - <<'EOF'\nimport os\nos.system('task" + "kill /T /PID 1')\nEOF\n"
    assert denied(run, tmp_path)


def test_a_live_kill_after_a_comment_still_denies(tmp_path):
    _write(tmp_path, "reap.ps1", "<# comment #>\n# note\ntaskkill /T /PID 4242\n")
    assert denied("pwsh -File reap.ps1", tmp_path, "PowerShell")


_LEG_DRIVE_SHAPE = (
    "function Get-BirthTicks([int]$Id_) { (Get-Process -Id $Id_).StartTime.ToUniversalTime().Ticks }\n"
    "function Get-SpawnTreeIds([int]$RootPid) {\n"
    "  $rows = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, CreationDate)\n"
    "  $order = New-Object System.Collections.ArrayList; [void]$order.Add($RootPid)\n"
    "  foreach ($r in ($rows | Where-Object { $_.ParentProcessId -eq $RootPid })) { [void]$order.Add([int]$r.ProcessId) }\n"
    "  return @($order)\n"
    "}\n"
    "function Stop-Spawned($Targets) {\n"
    "  foreach ($t in $Targets) {\n"
    "    if ((Get-BirthTicks $t.pid) -ne $t.birth) { continue }\n"
    "    $ids = @(Get-SpawnTreeIds ([int]$t.pid)); [array]::Reverse($ids)\n"
    "    foreach ($id in $ids) { Stop-Process -Id $id -Force }\n"
    "  }\n"
    "}\n"
    "function Get-ForeignPidDetail([int]$ForeignPid) {\n"
    "  $p = Get-CimInstance -ClassName Win32_Process -Filter \"ProcessId=$ForeignPid\"\n"
    "  [ordered]@{ pid = $ForeignPid; image = $p.Name }\n"
    "}\n"
    "Stop-Process -Id $RootPid -Force\n"
)


def test_a_script_killing_only_spawn_recorded_pids_passes_beside_a_lookup(tmp_path):
    _write(tmp_path, "leg-drive.ps1", _LEG_DRIVE_SHAPE)
    cmd = "mkdir -p run && pwsh -NoProfile -File leg-drive.ps1 -RunDir run -CommandLine x"
    assert denied(cmd, tmp_path) is None


@pytest.mark.parametrize("body", [
    "$ids = Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | ForEach-Object ProcessId\n"
    "foreach ($i in $ids) { Stop-Process -Id $i -Force }\n",
    "$conn = Get-NetTCPConnection -LocalPort 3000\n$owner = $conn.OwningProcess\nStop-Process -Id $owner\n",
    "PIDS=$(pgrep -f derive)\nfor p in $PIDS; do\n  kill -9 $p\ndone\n",
    "function Reap([int]$Root) { taskkill /F /T /PID $Root }\n",
])
def test_a_script_whose_kill_a_lookup_feeds_still_denies(tmp_path, body):
    _write(tmp_path, "reap.ps1", body)
    reason = denied("pwsh -File reap.ps1", tmp_path, "PowerShell")
    assert reason and "in script reap.ps1" in reason


@pytest.mark.parametrize("flag", ["-n", "-c", "-C 2", "--count"])
def test_a_search_for_kill_verbs_is_not_a_kill(tmp_path, flag):
    cmd = f'rg {flag} "Win32_Process|Stop-Process|taskkill|\\.Kill\\(|ParentProcessId" scripts/leg-drive.ps1'
    assert denied(cmd, tmp_path) is None


@pytest.mark.parametrize("command", [
    'bash -c "kill $(pgrep -f derive)"',
    'pwsh -NoProfile -Command "Get-Process node | Stop-Process"',
    "sh -c 'lsof -ti :3000 | xargs kill'",
])
def test_an_interpreter_inline_body_is_still_scanned(tmp_path, command):
    assert denied(command, tmp_path)


def test_a_quoted_assignment_value_is_not_a_script_run(tmp_path):
    _write(tmp_path, "reap.py", "import subprocess\nsubprocess.run(['taskkill', '/PID', '9', '/T', '/F'])\n")
    cmd = 'F="guard.py reap.py notes.md"; git commit -F msg.txt -- $F'
    assert denied(cmd, tmp_path) is None
    assert denied("F=1 python reap.py", tmp_path)


_RECOVER = '''import argparse, subprocess

def free_port(port):
    out = subprocess.check_output(["netstat", "-ano"], text=True)
    pid = out.split()[-1]
    subprocess.run(["taskkill", "/PID", pid, "/F"])

def rebuild_plugin():
    subprocess.run(["Build.bat"])

def recover_editor():
    free_port(8080)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step")
    ap.parse_args()

if __name__ == "__main__":
    main()
'''


@pytest.mark.parametrize("command", [
    "python3 recover.py --help",
    "python3 recover.py -h",
    "python3 recover.py --step rebuild-plugin",
    "python3 recover.py --step=rebuild-plugin",
])
def test_a_script_run_that_cannot_reach_its_kill_path_is_allowed(tmp_path, command):
    _write(tmp_path, "recover.py", _RECOVER)
    assert denied(command, tmp_path) is None


@pytest.mark.parametrize("command", [
    "python3 recover.py",
    "python3 recover.py --step free-port",
    "python3 recover.py --step recover-editor",
    "python3 recover.py --step unknown-step",
])
def test_a_script_run_that_reaches_its_kill_path_still_denies(tmp_path, command):
    _write(tmp_path, "recover.py", _RECOVER)
    reason = denied(command, tmp_path)
    assert reason and "in script recover.py" in reason


def test_a_kill_under_the_main_guard_denies_even_with_a_step(tmp_path):
    _write(tmp_path, "inline.py", _RECOVER + '\nif __name__ == "__main__":\n    pid = subprocess.check_output(["netstat", "-ano"], text=True).split()[-1]\n    subprocess.run(["taskkill", "/PID", pid, "/F"])\n')
    assert denied("python3 inline.py --step rebuild-plugin", tmp_path)


def test_help_does_not_lift_a_direct_kill_on_the_command_line(tmp_path):
    _write(tmp_path, "recover.py", _RECOVER)
    assert denied("python3 recover.py --help; kill -9 $(lsof -ti :3000)", tmp_path)


def test_script_not_run_or_not_recent_is_not_read(tmp_path):
    _write(tmp_path, "reap.sh", "pkill -f derive\n")
    _write(tmp_path, "old.sh", "pkill -f derive\n", age_s=3 * 24 * 3600)
    assert denied("cat reap.sh", tmp_path) is None
    assert denied("python -m pytest reap.sh", tmp_path) is None
    assert denied("bash old.sh", tmp_path) is None
    assert denied("bash missing.sh", tmp_path) is None


def test_heredoc_written_script_is_scanned_but_commit_body_is_not(tmp_path):
    written = "cat > reap.sh <<'EOF'\npkill -f derive\nEOF\nbash reap.sh"
    assert denied(written, tmp_path)
    message = "git commit -F - <<'EOF'\npkill -f derive was removed\nEOF"
    assert denied(message, tmp_path) is None


@pytest.mark.parametrize("command", [
    'echo "run taskkill /T /PID 4242 to reap the tree"',
    "echo 'pkill -f derive; kill $(pgrep derive)'",
    "cat > docs/note.md <<'EOF'\nNever run taskkill /T or pkill -f derive; kill $(lsof -ti :3000) is out.\nEOF",
    "git commit -m \"deny taskkill /T\" -m 'pkill -f derive'",
    'printf "%s" "Stop-Process -Name node"',
])
def test_quoted_or_heredoc_prose_naming_a_kill_is_not_a_kill(tmp_path, command):
    assert denied(command, tmp_path) is None


def test_the_override_key_lets_a_denied_command_through(tmp_path):
    cmd = "taskkill /PID 4242 /T /F"
    assert denied(cmd, tmp_path)
    assert denied(cmd, tmp_path, env={guard.OVERRIDE_ENV: "1"}) is None
    assert denied(cmd, tmp_path, env={guard.OVERRIDE_ENV: "0"})


def test_the_deny_is_a_hard_deny_naming_the_guard(tmp_path):
    record = guard.check({"tool_name": "Bash", "tool_input": {"command": "taskkill /T /PID 4"}, "cwd": str(tmp_path)})
    assert record.verdict_kind == "deny"
    assert record.guard_name == "deny-kill-by-port-or-tree"
    assert guard.CLASS == "hard-deny"


@pytest.mark.parametrize("payload", [
    {},
    {"tool_name": "Bash"},
    {"tool_name": "Bash", "tool_input": {"command": 7}},
    {"tool_name": "Bash", "tool_input": "taskkill /T"},
    {"tool_name": "Read", "tool_input": {"command": "taskkill /T /PID 4"}},
])
def test_unusable_payload_allows(payload):
    assert guard.check(payload) is None


def test_prefilter_p50_under_5ms(tmp_path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status --short && ls -la"},
               "cwd": str(tmp_path)}
    samples = []
    for _ in range(200):
        start = time.process_time()
        assert guard.check(payload) is None
        samples.append((time.process_time() - start) * 1000)
    assert statistics.median(samples) < 5


def _as_gem(monkeypatch, holder="S1"):
    from coordinator_core.group_em import nomination

    monkeypatch.setattr(nomination, "identify", lambda root: {"state": "live", "holder": {"session_id": holder}})


@pytest.mark.parametrize("command", [
    "Get-CimInstance Win32_Process -Filter 'ProcessId=48920'; Stop-Process -Id 48920 -Force",
    "taskkill /PID 4242 /T /F",
    "taskkill /IM node.exe /F",
    "pkill -f derive",
])
def test_live_gem_session_kill_is_allowed_and_others_still_denied(tmp_path, monkeypatch, command):
    _as_gem(monkeypatch)
    ident = {"session_id": "S1"}
    assert denied(command, tmp_path, **ident) is None
    assert denied(command, tmp_path, session_id="S2")
    assert denied(command, tmp_path, session_id="S1", agent_id="a1")
    assert denied(command, tmp_path)


def test_gem_save_box_prefix_does_not_exempt_a_non_gem_kill(tmp_path, monkeypatch):
    _as_gem(monkeypatch, holder="S2")
    command = "gem-save-box 1; taskkill /T /PID 4"
    assert denied(command, tmp_path, session_id="S1")
    assert denied(command, tmp_path, session_id="S2") is None


_GEM_SAVE_BOX_COMMANDS = [
    "python tools/gem-save-box/gem_save_box.py survey --json",
    "python tools/gem-save-box/gem_save_box.py kill --pid 4242 --reason runaway",
]


@pytest.mark.parametrize("command", _GEM_SAVE_BOX_COMMANDS)
def test_gem_save_box_runs_through_the_real_hook_for_the_gem_only(tmp_path, monkeypatch, command):
    import json

    from coordinator_core.bash_guards import dispatch

    _as_gem(monkeypatch)
    tool = tmp_path / "tools" / "gem-save-box"
    tool.mkdir(parents=True)
    (tool / "gem_save_box.py").write_text(
        "import os\nimport psutil\nfor p in psutil.process_iter():\n    p.kill()\nos.kill(4242, 9)\n"
    )

    def run(**ident):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path), **ident}
        return dispatch.evaluate_payload_json(json.dumps(payload))

    assert run(session_id="S1") is None
    assert run(session_id="S2")
    assert run(session_id="S1", agent_id="a1")


def test_crash_trigger_words_match_the_module_constant():
    from coordinator_core.bash_guards import dispatch

    assert dispatch._CRASH_TRIGGER_SUBSTRINGS[guard.GUARD_NAME] == guard.COMMAND_WORDS


def test_a_script_reaping_its_own_popen_tree_is_allowed(tmp_path):
    body = (
        "import subprocess\n"
        "proc = subprocess.Popen(['pytest'])\n"
        "subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)])\n"
    )
    _write(tmp_path, "run-tests.py", body)
    assert denied("python3 run-tests.py a.py -q", tmp_path) is None


def test_a_script_tree_kill_of_a_foreign_or_looked_up_pid_still_denies(tmp_path):
    _write(tmp_path, "a.py", "import subprocess\nsubprocess.run(['taskkill', '/T', '/PID', '4242'])\n")
    _write(tmp_path, "b.py", "import subprocess, psutil\nfor p in psutil.process_iter():\n"
           "    subprocess.run(['taskkill', '/T', '/PID', str(p.pid)])\n")
    assert denied("python a.py", tmp_path)
    assert denied("python b.py", tmp_path)


_REVISE = (
    'python "$HOME/.coordinator-claude-settings/bin/coordinator-invoke" workflow.revise '
    '\'{"run_id":"warp-d40d17-1","note":"EM: the uncommitted hunks in '
    'coordinator_core/bash_guards/dispatch.py (_VERDICT_ONLY_ENV, _record_deny_fire) '
    'C2 proceeds: cut the guard-chain eager imports, never by stashing"}\''
)


def test_a_script_name_in_a_json_argument_is_data_not_a_script_run(tmp_path):
    (tmp_path / "coordinator_core" / "bash_guards").mkdir(parents=True)
    _write(tmp_path / "coordinator_core" / "bash_guards", "dispatch.py", "import os\nos.system('pkill -f x')\n")
    assert denied(_REVISE, tmp_path) is None
    assert denied("python coordinator_core/bash_guards/dispatch.py", tmp_path)


def test_a_real_taskkill_by_image_name_still_denies_beside_the_revise_command(tmp_path):
    assert denied("taskkill /IM python.exe /F", tmp_path)
    assert denied(_REVISE + " && taskkill /IM python.exe /F", tmp_path)


def test_pgrep_found_pid_kill_is_routed_to_stop_own_child(tmp_path):
    reason = denied("pgrep -f sleep | xargs kill", tmp_path)
    assert "coordinator-invoke proc.stop_own_child" in reason


def test_denial_remedy_names_the_own_tree_route(tmp_path):
    reason = denied("kill $(pgrep -f pytest)", tmp_path)
    assert "coordinator-invoke proc.stop_own_child" in reason
    assert "your own process tree" in reason
