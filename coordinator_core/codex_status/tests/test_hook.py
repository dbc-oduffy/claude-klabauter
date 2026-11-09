from __future__ import annotations

import contextlib
import json
from pathlib import Path

from coordinator_core.codex_status import hook

SID = "01a1b63c-040a-76f2-a9b9-929d9a0d0098"


def value():
    return {"session_id": SID, "alias": "actual-route", "usd": 1.23, "priced_events": 3,
            "backlog_complete": True, "coverage": {"subagents": True, "workflows": True},
            "warnings": [], "unpriced_events": 0}


def test_metadata_title_write_requires_loaded_namespace_and_preserved_task(tmp_path, monkeypatch):
    calls = []
    class Connection:
        def request(self, method, params, deadline):
            calls.append((method, params))
            if method == "thread/loaded/list":
                return {"data": [SID]}
            if method == "thread/read":
                return {"thread": {"id": SID, "cwd": str(tmp_path), "name": "Original assigned work"}}
            return {}
    monkeypatch.setattr(hook.codex_app_server, "_connect", lambda *a, **kw: contextlib.nullcontext(Connection()))
    monkeypatch.setattr(hook.CodexAdapter, "proxy_argv", lambda self: ["native", "app-server", "proxy"])
    def preserve(sid, title):
        calls.append(("preserve", title))
        return {"session_id": sid, "original_task_title": title}
    monkeypatch.setattr(hook.external_registry, "preserve_task_title", preserve, raising=False)
    result = hook.apply_title(value(), str(tmp_path))
    assert result == {"updated": True, "title": "actual-route · ~$1.23"}
    assert [call[0] for call in calls] == ["thread/loaded/list", "thread/read", "preserve", "thread/name/set"]
    assert calls[-1][1] == {"threadId": SID, "name": "actual-route · ~$1.23"}


def test_other_namespace_never_renames_or_starts_a_thread(tmp_path, monkeypatch):
    calls = []
    class Connection:
        def request(self, method, params, deadline):
            calls.append(method)
            return {"data": ["some-other-thread"]}
    monkeypatch.setattr(hook.codex_app_server, "_connect", lambda *a, **kw: contextlib.nullcontext(Connection()))
    monkeypatch.setattr(hook.CodexAdapter, "proxy_argv", lambda self: ["native"])
    monkeypatch.setattr(hook.external_registry, "preserve_task_title", lambda *a: {}, raising=False)
    assert hook.apply_title(value(), str(tmp_path))["updated"] is False
    assert calls == ["thread/loaded/list"]


def test_unknown_alias_does_not_spawn_a_metadata_client(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "snapshot", lambda p: {**value(), "alias": None})
    def forbidden(*a, **kw):
        raise AssertionError("must not connect without a routing identity")
    monkeypatch.setattr(hook, "apply_title", forbidden)
    result = hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "Stop")
    assert result["updated"] is False
    assert result["reason"] == "no exact registered route alias"


def test_status_event_is_fail_open_and_never_ends_a_session(tmp_path, monkeypatch):
    def broken(*a, **kw):
        raise OSError("temporarily unavailable")
    monkeypatch.setattr(hook, "snapshot", broken)
    assert hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "SubagentStop")["updated"] is False
    assert hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "SessionEnd")["reason"] == "not a status refresh event"


def test_subagent_stop_binds_actual_child_metadata_and_preserves_fork_dedup(tmp_path, monkeypatch):
    from coordinator_core.codex_status.__main__ import snapshot
    child = "01a1b63c-040a-76f2-a9b9-929d9a0d0099"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    def meta(sid):
        return {"type": "session_meta", "payload": {"id": sid}}
    def context(turn):
        return {"type": "turn_context", "payload": {"turn_id": turn, "model": "gpt-6.1-sol", "effort": "low"}}
    def usage(inp, out):
        count = {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out, "total_tokens": inp + out}
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": count, "total_token_usage": count, "model_context_window": 475000}}}
    prefix = [meta(SID), context("root-turn"), usage(1000, 100)]
    parent_path, child_path = tmp_path / "parent.jsonl", tmp_path / "child.jsonl"
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in prefix))
    # The first real child metadata has no parent; copied parent metadata must
    # never replace the actual child UUID or be billed a second time.
    child_path.write_text("".join(json.dumps(row) + "\n" for row in [meta(child), *prefix, context("child-turn"), usage(200, 20)]))
    before = snapshot({"session_id": SID, "rollouts": [str(parent_path)]})
    payload = {"session_id": SID, "cwd": str(tmp_path), "agent_id": "not-a-session-identity",
               "transcript_path": str(parent_path), "agent_transcript_path": str(child_path)}
    first = hook.handle(payload, "SubagentStop")["snapshot"]
    second = hook.handle(payload, "SubagentStop")["snapshot"]
    assert before["usd"] == .003
    assert first["usd"] == second["usd"] == .0036
    assert first["threads"] == 2 and first["priced_events"] == 2
    assert first["subagent_stop_ownership"]["child_session_id"] == child
    assert first["subagent_stop_ownership"]["parent_session_id"] == SID


def test_subagent_stop_never_substitutes_parent_uuid_for_child(tmp_path):
    import pytest
    from coordinator_core.codex_status.collect import bind_subagent_stop
    from coordinator_core.codex_status.model import empty
    path = tmp_path / "wrong-child.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n")
    state = empty(SID)
    with pytest.raises(ValueError, match="parent, not a child"):
        bind_subagent_stop(state, SID, path)
    assert state["files"] == {} and state["threads"] == {}
