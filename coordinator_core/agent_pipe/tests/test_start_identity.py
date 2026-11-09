"""No real processes: detached and provider boundaries use captured environments."""
import json
from types import SimpleNamespace
import pytest
from coordinator_core.agent_pipe import __main__ as relay
from coordinator_core.ops import agent_pipe as ops
from coordinator_core.session import core


@pytest.mark.parametrize("agent,vendor", [("codex", "codex"), ("antigravity", "agy")])
def test_start_projects_caller_then_scrubs_at_both_boundaries(tmp_path, monkeypatch, agent, vendor):
    dirty = {"CODEX_THREAD_ID": "daemon", "CLAUDE_SESSION_ID": "daemon", "COORDINATOR_SESSION_ID": "daemon",
             "COORDINATOR_AGENT_ROLE": "em", "COORDINATOR_AGENT_MODEL": "parent-model", "PATH": "preserved"}
    monkeypatch.setattr(core, "subprocess_identity_env", lambda **kw: {**dirty, "CODEX_THREAD_ID": "actual-caller"})
    monkeypatch.setattr(core, "attributable_session_id", lambda: "actual-caller")
    monkeypatch.setattr(ops.contract, "run_dir", lambda _: tmp_path / "run")
    monkeypatch.setattr(ops.contract, "mailbox_path", lambda _: tmp_path / "mailbox")
    captured = []
    monkeypatch.setattr(ops.subprocess, "Popen", lambda *a, **kw: captured.append(kw) or SimpleNamespace(pid=3))
    result = ops._spawn("agent_pipe.start", {"agent": agent, "reply_to": "recipient"}, {"prompt": "hello"})
    request = json.loads((tmp_path / "run" / "request.json").read_text())
    assert request["caller_session_id"] == "actual-caller"
    first = captured[0]["env"]
    assert first["COORDINATOR_PARENT_SESSION_ID"] == "actual-caller"
    assert first["COORDINATOR_AGENT_ROLE"] == "worker"
    assert "CODEX_THREAD_ID" not in first and "COORDINATOR_AGENT_MODEL" not in first
    monkeypatch.setattr(relay.os, "environ", {**dirty, **first, "CODEX_THREAD_ID": "relay-stale"})
    child = SimpleNamespace(stdout=[], wait=lambda: 0, poll=lambda: 0)
    monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **kw: captured.append(kw) or child)
    adapter = SimpleNamespace(spawn_argv=lambda p: ["fake"], parse_line=lambda s: None)
    relay._relay(tmp_path / "run", request, adapter)
    final = captured[-1]["env"]
    assert final["COORDINATOR_AGENT_VENDOR"] == vendor
    assert final["COORDINATOR_PARENT_SESSION_ID"] == "actual-caller"
    assert final["COORDINATOR_AGENT_ROLE"] == "worker"
    assert all(k not in final for k in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "COORDINATOR_SESSION_ID", "COORDINATOR_AGENT_MODEL"))


def test_existing_resume_keeps_identity_semantics(tmp_path, monkeypatch):
    current = {"CODEX_THREAD_ID": "existing-thread", "COORDINATOR_AGENT_ROLE": "em"}
    monkeypatch.setattr(relay.os, "environ", current)
    captured = []
    child = SimpleNamespace(stdout=[], wait=lambda: 0, poll=lambda: 0)
    monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **kw: captured.append(kw) or child)
    adapter = SimpleNamespace(resume_argv=lambda *a: ["fake", "resume"], parse_line=lambda s: None)
    relay._relay(tmp_path, {"agent": "codex", "reply_to": "recipient", "conversation": "existing-thread", "text": "hi"}, adapter)
    assert captured[0]["env"] == current
