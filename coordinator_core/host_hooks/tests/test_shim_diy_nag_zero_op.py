"""Zero-op replay: a non-Claude EM that hand-edits and never invokes an op is nagged through the shim, never denied."""
from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core import warp_fire
from coordinator_core.host_hooks import shim
from coordinator_core.session import op_dispatch_nag as nag
from coordinator_core.warm import telemetry

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NAG = "NAG-TEXT"


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, creationflags=_NO_WINDOW)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for i in range(3):
        (root / f"f{i}.txt").write_text("a\n" * 5)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    for i in range(3):
        (root / f"f{i}.txt").write_text("b\n" * 40)
    monkeypatch.setattr(nag.xs_lane_budget, "lane_path", lambda r, sid: tmp_path / "hub" / sid / "lane.json")
    monkeypatch.setattr(warp_fire, "nag_text", lambda sid, sizing_rel=None, state_dir=None, host=None: NAG)
    for key in ("COORDINATOR_AGENT_PROVIDER", "COORDINATOR_AGENT_VENDOR", "COORDINATOR_AGENT_ROLE",
                "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "AGY_SESSION_ID", "COORDINATOR_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(telemetry, "record_degrade", lambda **kw: None)
    return root


def _payload(host, root, sid):
    if host == "agy":
        return {"conversationId": sid, "workspacePaths": [str(root)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
    return {"hook_event_name": "PreToolUse", "session_id": sid, "cwd": str(root),
            "tool_name": "exec_command" if host == "codex" else "Bash", "tool_input": {"command": "ls"}}


def _shows_nag(host, out):
    if host == "agy":
        return out.get("decision") == "ask" and NAG in out.get("reason", "")
    return NAG in str(out) and "deny" not in str(out)


@pytest.mark.parametrize("engine", ["down", "allow"])
@pytest.mark.parametrize("host", ["codex", "agy", "grok"])
def test_zero_op_session_gets_advisory_then_throttled(monkeypatch, repo, host, engine):
    monkeypatch.setattr(shim, "_dial", lambda request: None if engine == "down" else {"result": {}})
    first = shim.run(host, json.dumps(_payload(host, repo, "zero-op")))
    assert _shows_nag(host, first), first
    spawns = []
    real = subprocess.run
    monkeypatch.setattr(nag.subprocess, "run", lambda *a, **k: spawns.append(a) or real(*a, **k))
    second = shim.run(host, json.dumps(_payload(host, repo, "zero-op")))
    assert not _shows_nag(host, second), second
    assert spawns == []


def test_engine_deny_is_untouched(monkeypatch, repo):
    deny = {"permissionDecision": "deny", "permissionDecisionReason": "no"}
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": deny})
    out = shim.run("codex", json.dumps(_payload("codex", repo, "deny-sid")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert NAG not in str(out)


def test_existing_advisory_is_merged(monkeypatch, repo):
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": {"additionalContext": "OLD"}})
    out = shim.run("codex", json.dumps(_payload("codex", repo, "merge-sid")))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "OLD" in text and NAG in text


@pytest.mark.parametrize("event", ["PostToolUse", "Stop", "SessionStart"])
def test_non_pretooluse_fire_gets_no_nag(monkeypatch, repo, event):
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": {"additionalContext": "OLD"}})
    event_nag = shim._nag_text("codex", shim._adapter("codex").adapt_event({**_payload("codex", repo, "ev-sid"), "hook_event_name": event}),
                               {})
    assert event_nag == ""


def test_subagent_fire_gets_no_nag(monkeypatch, repo):
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": {}})
    payload = {**_payload("codex", repo, "sub-sid"), "agent_id": "child"}
    assert NAG not in str(shim.run("codex", json.dumps(payload)))
