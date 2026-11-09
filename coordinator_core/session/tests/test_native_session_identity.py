"""Native session identities use the existing baton, PM ledger and warm caller seam."""
from __future__ import annotations

import uuid

import pytest

from coordinator_core.session import core
from coordinator_core.warm.caller_context import CallerContext, merge_env_axis
from coordinator_core.ops import baton_pm_turns, session_baton_mint

SID = "01a1b60c-a65b-7692-afc3-b93eabd93b04"
OTHER = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def identity_env(monkeypatch):
    for name in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name", core.NATIVE_SESSION_ENV_PRECEDENCE)
def test_native_uuid_is_preserved_and_beats_inherited_claude(monkeypatch, name):
    monkeypatch.setenv(name, SID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OTHER)
    assert core.resolve_session_id() == SID
    monkeypatch.setenv("COORDINATOR_SESSION_ID", OTHER)
    assert core.resolve_session_id() == OTHER


def test_opaque_native_session_has_stable_vendor_namespaced_uuid(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_VENDOR", "grok")
    monkeypatch.setenv("COORDINATOR_AGENT_SESSION", "conversation-7")
    first = core.resolve_session_id()
    assert str(uuid.UUID(first)) == first
    assert core.resolve_session_id() == first
    monkeypatch.setenv("COORDINATOR_AGENT_VENDOR", "agy")
    assert core.resolve_session_id() != first


@pytest.mark.parametrize("native", core.NATIVE_SESSION_ENV_PRECEDENCE)
def test_warm_env_axis_carries_native_identity_without_inheriting_server(monkeypatch, native):
    monkeypatch.setenv("CLAUDE_SESSION_ID", OTHER)
    caller = CallerContext(plugin_root=None, cwd=None, session_id=None, agent_id=None, pid=None)
    merged = merge_env_axis(caller, {native: SID})
    assert merged.session_id == SID
    assert merged.env["COORDINATOR_SESSION_ID"] == SID


def test_warm_opaque_identity_uses_wire_vendor_not_spawner(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_VENDOR", "agy")
    caller = CallerContext(plugin_root=None, cwd=None, session_id=None, agent_id=None, pid=None)
    env = {"COORDINATOR_AGENT_VENDOR": "grok", "COORDINATOR_AGENT_SESSION": "opaque-7"}
    merged = merge_env_axis(caller, env)
    assert merged.session_id == core.canonical_session_env_value(
        "COORDINATOR_AGENT_SESSION", "opaque-7", vendor="grok")


def test_subprocess_does_not_manufacture_native_vendor_slots(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", SID)
    child = core.subprocess_identity_env()
    assert all(name not in child for name in core.NATIVE_SESSION_ENV_PRECEDENCE)
    monkeypatch.setenv("CODEX_THREAD_ID", SID)
    child = core.subprocess_identity_env()
    assert child["CODEX_THREAD_ID"] == SID
    assert "ANTIGRAVITY_CONVERSATION_ID" not in child


def test_external_baton_bootstrap_then_verbatim_pm_ledger(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="utf-8")
    hub = tmp_path / "session-hub"
    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: str(hub))
    monkeypatch.setattr(core, "git_root", lambda cwd=None: str(tmp_path))
    monkeypatch.setenv("CODEX_THREAD_ID", SID)
    result = session_baton_mint._handler(
        {"session_id": SID, "cwd": str(tmp_path), "initialize": True, "prompt": "PM launch"})
    assert result["exit_code"] == 0
    assert (hub / SID / "meta.json").is_file()
    prompt = "  Keep the PM's words verbatim.\nSecond line.\n"
    appended = baton_pm_turns.append_turn(prompt, cwd=str(tmp_path))
    assert appended["ok"] and appended["session_id"] == SID
    assert baton_pm_turns.read_turns(cwd=str(tmp_path))["text"] == prompt
    assert baton_pm_turns.verify_pm_ask(prompt, cwd=str(tmp_path))["verdict"] == "matched"


@pytest.mark.parametrize("sid", ["../outside", "nested/session", "..", "nested\\session"])
def test_bootstrap_rejects_session_path_escape(tmp_path, sid):
    result = session_baton_mint._handler({"session_id": sid, "cwd": str(tmp_path), "initialize": True})
    assert result["exit_code"] == 1
