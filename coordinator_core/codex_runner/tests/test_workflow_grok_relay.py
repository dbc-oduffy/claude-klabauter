"""Grok op-relay stage: scoped coordinator-invoke allow grant, plain-command Windows preamble, named cancel error.

The grok binary is a stub script under tmp_path; the real CLI is never invoked.
"""
import json
import subprocess
import sys

import pytest

from coordinator_core.codex_runner import workflow

_STUB = """\
import json, os, sys
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
sys.stdout.write(os.environ["STUB_ENVELOPE"])
"""
_SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


@pytest.fixture
def stub(tmp_path, monkeypatch):
    script = tmp_path / "stub_grok.py"
    script.write_text(_STUB, encoding="utf-8")
    seen = []

    def spawn(argv, prompt, *, env, repo_root, stdout, stderr, **_kw):
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


def test_relay_child_gets_only_the_coordinator_invoke_allow_rule(tmp_path, stub):
    seen, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    assert _run(tmp_path, "coordinator:op-relay") == {"a": 1}
    argv = seen[0]
    assert _allow(argv) == "Bash(coordinator-invoke:*)"
    assert argv.count("--allow") == 1
    assert not {"--always-approve", "--dangerously-skip-permissions", "--yolo"} & set(argv)


def test_other_grok_stages_keep_the_incumbent_allow(tmp_path, stub):
    seen, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": '{"a": 1}', "stopReason": "end_turn"}))
    assert _run(tmp_path, "coordinator:executor") == {"a": 1}
    assert _allow(seen[0]) == "Bash"


def test_cancelled_stop_reason_fails_with_named_error_carrying_stderr(tmp_path, stub):
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": "", "stopReason": "cancelled"}),
               STUB_STDERR="permission required: Execute 'Set-Location x' was not run")
    with pytest.raises(workflow.AgentFailed) as caught:
        _run(tmp_path, "coordinator:op-relay")
    message = str(caught.value)
    assert "cancelled" in message and "permission required" in message
    assert "no schema-valid result" not in message


def test_cancelled_without_stderr_still_names_cancellation(tmp_path, stub):
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": "", "stopReason": "cancelled"}))
    with pytest.raises(workflow.AgentFailed, match="cancelled.*no stderr reason"):
        _run(tmp_path, "coordinator:op-relay")


def test_empty_text_with_other_stop_reason_keeps_the_schema_error(tmp_path, stub):
    _, box = stub
    box.update(STUB_ENVELOPE=json.dumps({"text": "", "stopReason": "end_turn"}))
    with pytest.raises(workflow.AgentFailed, match="no schema-valid result"):
        _run(tmp_path, "coordinator:op-relay")


@pytest.mark.parametrize("os_name,plain", [("nt", True), ("posix", False)])
def test_windows_preamble_demands_one_plain_command(tmp_path, stub, monkeypatch, os_name, plain):
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
