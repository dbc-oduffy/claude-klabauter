"""Grok op-relay stage: scoped coordinator-invoke allow grant, plain-command Windows preamble, named cancel error.

The grok binary is a stub script under tmp_path; the real CLI is never invoked.
"""
import json
import os
import re
import subprocess
import sys

import pytest

from coordinator_core.codex_runner import workflow
from pathlib import Path
from typing import TYPE_CHECKING

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

if TYPE_CHECKING:
    from subprocess import Popen

_STUB = """\
import json, os, sys
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
sys.stdout.write(os.environ["STUB_ENVELOPE"])
"""
_SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "stub_grok.py"
    script.write_text(_STUB, encoding="utf-8")
    seen = []

    def spawn(argv, prompt, *, env, repo_root, stdout, stderr, **_kw) -> "Popen[bytes]":
        seen.append(list(argv))
        child = subprocess.Popen([sys.executable, str(script)], env={**env, **_ENV_BOX}, stdout=stdout,
                                 stderr=stderr, cwd=repo_root)
        child.wait()
        return child

    _ENV_BOX = {}
    monkeypatch.setattr(workflow, "_run_admitted", spawn)
    monkeypatch.setattr(workflow, "_persona", lambda _agent_type: "")
    monkeypatch.setattr(workflow, "resolve_grok", lambda: [str(script)])
    return seen, _ENV_BOX


def _run(tmp_path, agent_type):
    return workflow.run_agent("relay it", {"schema": _SCHEMA, "agentType": agent_type}, repo_root=tmp_path,
                              artifact_dir=tmp_path / "art", backend="grok", parent_session_id="sid")


def _allow(argv):
    return argv[argv.index("--allow") + 1]


def test_grok_stage_maps_sonnet_tier_and_named_effort(tmp_path, stub):
    seen, box = stub
    box["STUB_ENVELOPE"] = json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"})
    workflow.run_agent(
        "relay it",
        {"schema": _SCHEMA, "model": "sonnet", "effort": "medium"},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", backend="grok", parent_session_id="sid",
    )
    argv, = seen
    assert argv[argv.index("--model") + 1] == "grok-4.7-build-fast"
    assert argv[argv.index("--reasoning-effort") + 1] == "medium"
    assert "--no-subagents" in argv
    receipt = json.loads((tmp_path / "art/process.json").read_text(encoding="utf-8"))
    assert receipt["requested_model"] == "grok-4.7-build-fast"
    assert receipt["requested_effort"] == "medium"
    assert "search_replace" in (tmp_path / "art/prompt.txt").read_text(encoding="utf-8")


def test_grok_stage_rejects_an_unknown_effort(tmp_path, stub):
    with pytest.raises(workflow.AgentFailed, match="reasoning effort"):
        workflow.run_agent(
            "relay it", {"schema": _SCHEMA, "effort": "ultra"},
            repo_root=tmp_path, artifact_dir=tmp_path / "art", backend="grok", parent_session_id="sid",
        )


def test_grok_stage_leaves_model_and_effort_to_the_cli_when_omitted(tmp_path, stub):
    seen, box = stub
    box["STUB_ENVELOPE"] = json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"})
    workflow.run_agent(
        "relay it", {"schema": _SCHEMA},
        repo_root=tmp_path, artifact_dir=tmp_path / "art", backend="grok", parent_session_id="sid",
    )
    argv, = seen
    assert "--model" not in argv and "--reasoning-effort" not in argv


def test_grok_sizing_uses_native_progress_and_reads_terminal_result(tmp_path, stub):
    seen, box = stub
    box["STUB_ENVELOPE"] = "\n".join(json.dumps(event) for event in (
        {"type": "available_commands", "commands": []},
        {"type": "text", "data": '{"a":'},
        {"type": "text", "data": " 1}"},
        {"type": "end", "stopReason": "end_turn", "modelUsage": {"grok-build": {"modelCalls": 1}}},
    ))

    assert _run(tmp_path, "coordinator:sizing-scout") == {"a": 1}
    argv, = seen
    assert argv[argv.index("--output-format") + 1] == "streaming-json"
    receipt = json.loads((tmp_path / "art/process.json").read_text(encoding="utf-8"))
    assert receipt["model"] == "grok-build"


def test_grok_sizing_never_accepts_a_partial_stream_as_a_result(tmp_path, stub):
    _, box = stub
    box["STUB_ENVELOPE"] = json.dumps({"type": "text", "data": '{"a":1}'}) + "\n"
    with pytest.raises(workflow.AgentFailed, match="grok emitted no terminal end event"):
        _run(tmp_path, "coordinator:sizing-scout")


def test_grok_sizing_names_native_stream_cancellation(tmp_path, stub):
    _, box = stub
    box["STUB_ENVELOPE"] = "\n".join(json.dumps(event) for event in (
        {"type": "text", "data": '{"a":1}'},
        {"type": "end", "stopReason": "cancelled"},
    ))
    box["STUB_STDERR"] = "permission required"
    with pytest.raises(workflow.AgentFailed, match="stopReason cancelled.*permission required"):
        _run(tmp_path, "coordinator:sizing-scout")


def test_relay_child_gets_only_the_coordinator_invoke_allow_rule(tmp_path: Path, stub) -> None:
    seen, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    assert _run(tmp_path, "coordinator:op-relay") == {"a": 1}
    argv = seen[0]
    assert _allow(argv) == _invoke_allow()
    assert argv.count("--allow") == 1
    assert not {"--always-approve", "--dangerously-skip-permissions", "--yolo"} & set(argv)


def _invoke_allow() -> str:
    """The one coordinator-invoke rule a POSIX grok child is given: the absolute launcher."""
    if os.name == "nt":
        return workflow._RELAY_ALLOW
    launcher = (workflow.settings_home() / "bin" / "coordinator-invoke").as_posix()
    return f"Bash({launcher}:*)"


def _rule_matches(rule, command):
    """grok's `Bash(<prefix>:*)` grammar: the command is the prefix or the prefix then a space."""
    assert rule.startswith("Bash(") and rule.endswith(":*)")
    prefix = rule[len("Bash("):-len(":*)")]
    return command == prefix or command.startswith(prefix + " ")


_WINDOWS_FULL_PATH_CALL = ('& "C:\\Users\\x\\.coordinator-claude-settings\\bin\\coordinator-invoke.exe" '
                           "dispatch.land_run --params-file C:/x/p.json")


def test_commit_stage_child_gets_only_the_coordinator_invoke_allow_rule(tmp_path: Path, stub) -> None:
    seen, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    assert _run(tmp_path, "coordinator:git-commit-agent") == {"a": 1}
    argv = seen[0]
    assert _allow(argv) == _invoke_allow()
    assert argv.count("--allow") == 1
    assert not {"--always-approve", "--dangerously-skip-permissions", "--yolo"} & set(argv)


def test_the_rule_admits_the_bare_command_the_commit_prompt_emits_and_nothing_wider() -> None:
    from coordinator_core.ops.dispatch_emit import ask_compose

    call = ask_compose._commit_stage_call(head="h", anchor="a", repo_root="C:/x", run_id="ask-20261110T075901-ebd02d")
    emitted = re.findall(r"coordinator-invoke dispatch\.land_run --params-file \S+ --repo \S+", call)
    assert emitted, "commit prompt emits no bare coordinator-invoke command"
    for command in emitted:
        assert _rule_matches(workflow._RELAY_ALLOW, command)
    assert _rule_matches(workflow._RELAY_ALLOW, "coordinator-invoke dispatch.land_run --params-file C:/x/p.json")
    assert not _rule_matches(workflow._RELAY_ALLOW, _WINDOWS_FULL_PATH_CALL)
    assert not _rule_matches(workflow._RELAY_ALLOW, "git status")
    assert not _rule_matches(workflow._RELAY_ALLOW, "coordinator-invoke-evil x")


def test_windows_preamble_emits_the_bare_form_the_rule_matches(tmp_path: Path, stub, monkeypatch: pytest.MonkeyPatch) -> None:
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    real_os = workflow.os

    class _OsView:
        name = "nt"

        def __getattr__(self, attr):
            return getattr(real_os, attr)

    monkeypatch.setattr(workflow, "os", _OsView())
    _run(tmp_path, "coordinator:git-commit-agent")
    prompt = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    assert "by that bare name" in prompt and "never its full path or the & operator" in prompt
    assert "run no cd or Set-Location" in prompt


def test_grok_child_path_leads_with_the_settings_home_bin(tmp_path: Path, stub, monkeypatch: pytest.MonkeyPatch) -> None:
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    seen_env = []
    inner = workflow._run_admitted

    def spy(argv, prompt, *, env, **kw):
        seen_env.append(env["PATH"])
        return inner(argv, prompt, env=env, **kw)

    monkeypatch.setattr(workflow, "_run_admitted", spy)
    _run(tmp_path, "coordinator:git-commit-agent")
    assert seen_env[0].split(workflow.os.pathsep)[0] == str(workflow.settings_home() / "bin")


@pytest.mark.parametrize("git_on_path", [True, False])
def test_grok_child_path_gains_git_dir_only_when_engine_path_lacks_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_on_path) -> None:
    program_files = tmp_path / "pf"
    (program_files / "Git" / "cmd").mkdir(parents=True)
    (program_files / "Git" / "cmd" / "git.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setattr(workflow.shutil, "which", lambda name, path=None: "git" if git_on_path else None)
    parts = workflow._grok_child_path({"PATH": "base"}).split(workflow.os.pathsep)
    assert parts[0] == str(workflow.settings_home() / "bin") and parts[1] == "base"
    assert (str(program_files / "Git" / "cmd") in parts) is (not git_on_path)


@pytest.mark.parametrize("git_on_path", [True, False])
def test_run_agent_hands_the_grok_child_a_path_with_git_only_when_the_engine_lacks_it(
        tmp_path: Path, stub, monkeypatch: pytest.MonkeyPatch, git_on_path) -> None:
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    program_files = tmp_path / "pf"
    (program_files / "Git" / "cmd").mkdir(parents=True)
    (program_files / "Git" / "cmd" / "git.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setattr(workflow.shutil, "which",
                        lambda name, path=None: "git" if git_on_path and name == "git" else None)
    seen_env = []
    inner = workflow._run_admitted

    def spy(argv, prompt, *, env, **kw):
        seen_env.append(env["PATH"])
        return inner(argv, prompt, env=env, **kw)

    monkeypatch.setattr(workflow, "_run_admitted", spy)
    _run(tmp_path, "coordinator:git-commit-agent")
    parts = seen_env[0].split(workflow.os.pathsep)
    assert parts[0] == str(workflow.settings_home() / "bin")
    assert (str(program_files / "Git" / "cmd") in parts) is (not git_on_path)


def test_other_grok_stages_keep_the_incumbent_allow(tmp_path: Path, stub) -> None:
    seen, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    assert _run(tmp_path, "coordinator:executor") == {"a": 1}
    assert _allow(seen[0]) == "Bash"
    mode = seen[0][seen[0].index("--permission-mode") + 1]
    assert mode == ("bypassPermissions" if os.name != "nt" else "acceptEdits")


def test_cancelled_stop_reason_fails_with_named_error_carrying_stderr(tmp_path: Path, stub) -> None:
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": "", "stopReason": "cancelled"}),
               STUB_STDERR="permission required: Execute 'Set-Location x' was not run")
    with pytest.raises(workflow.AgentFailed) as caught:
        _run(tmp_path, "coordinator:op-relay")
    message = str(caught.value)
    assert "cancelled" in message and "permission required" in message
    assert "no schema-valid result" not in message


def test_cancelled_without_stderr_still_names_cancellation(tmp_path: Path, stub) -> None:
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": "", "stopReason": "cancelled"}))
    with pytest.raises(workflow.AgentFailed, match="cancelled.*no stderr reason"):
        _run(tmp_path, "coordinator:op-relay")


def test_empty_text_with_other_stop_reason_keeps_the_schema_error(tmp_path: Path, stub) -> None:
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": "", "stopReason": "end_turn"}))
    with pytest.raises(workflow.AgentFailed, match="no schema-valid result"):
        _run(tmp_path, "coordinator:op-relay")


@pytest.mark.parametrize("os_name,plain", [("nt", True), ("posix", False)])
def test_windows_preamble_demands_one_plain_command(tmp_path: Path, stub, monkeypatch: pytest.MonkeyPatch, os_name, plain) -> None:
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    real_os = workflow.os

    class _OsView:
        name = os_name

        def __getattr__(self, attr):
            return getattr(real_os, attr)

    monkeypatch.setattr(workflow, "os", _OsView())
    _run(tmp_path, "coordinator:op-relay")
    prompt = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    assert ("no Set-Location" in prompt) is plain


@pytest.mark.parametrize("backend,leads_with_amp", [("grok", False), ("codex", True)])
def test_windows_grok_is_told_the_bare_form_its_allow_gate_matches(
        tmp_path: Path, stub, monkeypatch: pytest.MonkeyPatch, backend, leads_with_amp) -> None:
    """grok's headless gate denies `& '<exe>'` even under --allow Bash, so only grok drops the call operator."""
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    real_os = workflow.os

    class _OsView:
        name = "nt"

        def __getattr__(self, attr):
            return getattr(real_os, attr)

    monkeypatch.setattr(workflow, "os", _OsView())
    monkeypatch.setattr(workflow, "_run_admitted", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        workflow.run_agent("go", {"agentType": "coordinator:executor"}, repo_root=tmp_path,
                           artifact_dir=tmp_path / "art", backend=backend, parent_session_id="sid")
    prompt = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    assert ("use the PowerShell call operator &" in prompt) is leads_with_amp
    assert ("by bare name" in prompt) is (not leads_with_amp)
