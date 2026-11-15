"""Pins for the Codex-as-EM proof harness with codex stubbed (no codex binary needed)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unimatrix_codex_em_proof", BIN / "unimatrix-codex-em-proof.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)

REPLY = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def _events(reply=REPLY, thread="thr-123"):
    evs = [{"type": "thread.started", "thread_id": thread},
           {"type": "item.completed", "item": {"type": "command_execution",
                                               "aggregated_output": json.dumps(reply, indent=2)}}]
    return "\n".join(json.dumps(e) for e in evs) + "\n"


@pytest.fixture
def home(tmp_path):
    bin_dir = tmp_path / "home" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "coordinator-invoke").write_text("#!/bin/sh\n")
    return tmp_path / "home"


def _run_main(tmp_path, home, stdout, code=0, timeout_exc=False):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw["input"]
        if timeout_exc:
            raise subprocess.TimeoutExpired(argv, kw["timeout"], output=stdout.encode())
        return subprocess.CompletedProcess(argv, code, stdout, "")

    audit = tmp_path / "audit.md"
    rc = proof.main(["--audit", str(audit)], run=fake_run, which=lambda n: "/stub/codex",
                    env={"COORDINATOR_SETTINGS_HOME": str(home)})
    return rc, audit, seen


def test_pass_path_records_session_request_op_and_exits_zero(tmp_path, home):
    rc, audit, seen = _run_main(tmp_path, home, _events())
    text = audit.read_text()
    assert rc == 0
    assert "outcome: pass" in text and 'session_id: "thr-123"' in text
    assert "request_id: 1" in text and 'op: "ping"' in text
    assert seen["argv"][:2] == ["/stub/codex", "exec"] and "read-only" in seen["argv"]
    assert "coordinator-invoke" in seen["input"] and "ping" in seen["input"]


@pytest.mark.parametrize("stdout,code,timeout_exc,reason", [
    (_events(), 3, False, "codex exited 3"),
    (_events(reply={"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}), 0, False,
     "engine reply is not ok"),
    (json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n", 0, False, "no engine reply"),
    (json.dumps({"type": "item.completed", "item": {"aggregated_output": json.dumps(REPLY)}}) + "\n", 0, False,
     "no session id"),
    (json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n"
     + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(REPLY)}}) + "\n",
     0, False, "not from a command execution"),
    (_events(), 0, True, "wall timeout"),
])
def test_fail_paths_are_recorded_and_exit_nonzero(tmp_path, home, stdout, code, timeout_exc, reason):
    rc, audit, _ = _run_main(tmp_path, home, stdout, code, timeout_exc)
    text = audit.read_text()
    assert rc == 1 and "outcome: fail" in text and reason in text


def test_missing_settings_home_is_a_typed_refusal_naming_the_key(tmp_path, capsys):
    rc = proof.main(["--audit", str(tmp_path / "a.md")], which=lambda n: "/stub/codex",
                    env={"COORDINATOR_SETTINGS_HOME": "", "CLAUDE_HOME": str(tmp_path)})
    assert rc == 2 and "COORDINATOR_SETTINGS_HOME" in capsys.readouterr().err
    assert not (tmp_path / "a.md").exists()


def test_absent_codex_exits_127_without_audit(tmp_path, home):
    rc = proof.main(["--audit", str(tmp_path / "a.md")], which=lambda n: None,
                    env={"COORDINATOR_SETTINGS_HOME": str(home)})
    assert rc == 127 and not (tmp_path / "a.md").exists()
