"""Grok EM/subagent split through `shim.decide`: `agent_id` selects the subagent limits, its absence the EM path.

The warm engine is stubbed to no verdict so the cold chain answers; no live engine is dialled.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.host_hooks import shim

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process]

_AGENT_ID = "a7889dad168f41aa5"
_NAG = "[em-code-dispatch nudge]"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture
def cold(monkeypatch, tmp_path):
    monkeypatch.setattr(shim, "_budgeted", lambda request, budget: ({}, False))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm"))
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("remote", "add", "origin", "https://github.com/me/thing.git")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, creationflags=_NO_WINDOW)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("u", encoding="utf-8")
    return repo


def _payload(repo: Path, tool_name: str, tool_input: dict, subagent: bool) -> dict:
    payload = {"hook_event_name": "PreToolUse", "session_id": "grok-split-s", "cwd": str(repo),
               "tool_name": tool_name, "tool_input": tool_input}
    if subagent:
        payload["agent_id"] = _AGENT_ID
    return payload


def _out(answer: dict) -> dict:
    return answer.get("hookSpecificOutput", {})


def test_write_leg_em_gets_the_diy_nag_and_a_subagent_gets_none(cold):
    args = {"path": str(cold / "a.py"), "old_string": "x = 1", "new_string": "x = 2"}
    em = _out(shim.decide("grok", _payload(cold, "edit_file", args, subagent=False)))
    assert em.get("permissionDecision") == "allow", em
    assert _NAG in em.get("additionalContext", ""), em
    sub = shim.decide("grok", _payload(cold, "edit_file", args, subagent=True))
    assert _NAG not in str(sub), sub


def test_shell_leg_a_subagent_only_deny_fires_with_agent_id_and_not_without(cold):
    args = {"command": "git clean -fdx"}
    sub = _out(shim.decide("grok", _payload(cold, "run_command", args, subagent=True)))
    assert sub.get("permissionDecision") == "deny", sub
    em = shim.decide("grok", _payload(cold, "run_command", args, subagent=False))
    assert _out(em).get("permissionDecision") != "deny", em


def test_live_shape_child_payload_with_subagent_type_is_a_subagent_and_em_spawn_is_not(cold):
    child = {"hook_event_name": "PreToolUse", "session_id": "child-sess-1", "cwd": str(cold),
             "tool_name": "run_command", "tool_input": {"command": "git clean -fdx"}, "subagentType": "general"}
    assert _out(shim.decide("grok", child)).get("permissionDecision") == "deny"
    em = {"hook_event_name": "PreToolUse", "session_id": "em-sess-1", "cwd": str(cold),
          "tool_name": "spawn_subagent", "tool_input": {"subagent_type": "general", "prompt": "p"}}
    assert _out(shim.decide("grok", em)).get("permissionDecision") != "deny"
    em_shell = {**child, "session_id": "em-sess-1"}
    del em_shell["subagentType"]
    assert _out(shim.decide("grok", em_shell)).get("permissionDecision") != "deny"
