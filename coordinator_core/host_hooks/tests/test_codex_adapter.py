"""Codex adapter: payload mapping, engine-event shape, verdict rendering, unserved events."""
from __future__ import annotations

from coordinator_core.host_hooks.codex_adapter import SERVED_EVENTS, CodexAdapter, verdict_parts
from coordinator_core.host_hooks.contract import Adapter, neutral_fallback
from coordinator_core.warm import hook_http

PAYLOAD = {
    "session_id": "codex-sess-1",
    "hook_event_name": "PreToolUse",
    "cwd": "/work/repo",
    "tool_name": "Bash",
    "tool_input": {"command": "git push origin main"},
}


def test_conforms_to_adapter_protocol():
    assert isinstance(CodexAdapter(), Adapter)


def test_adapt_event_takes_session_from_payload_not_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session")
    ev = CodexAdapter().adapt_event(PAYLOAD)
    assert ev.session_id == "codex-sess-1"
    assert (ev.host, ev.event, ev.tool_name, ev.cwd) == ("codex", "PreToolUse", "Bash", "/work/repo")
    assert ev.tool_input == {"command": "git push origin main"}


def test_adapt_event_missing_session_is_empty_never_fabricated(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session")
    ev = CodexAdapter().adapt_event({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert ev.session_id == ""
    assert CodexAdapter().engine_event(ev)["session_id"] is None


def test_exec_command_alias_reaches_bash_guards_as_bash():
    ev = CodexAdapter().adapt_event({**PAYLOAD, "tool_name": "exec_command"})
    assert ev.tool_name == "Bash"


def test_served_events_match_hook_http():
    assert SERVED_EVENTS == hook_http.SERVED_EVENTS


def test_engine_event_is_framed_by_build_request_with_payload_session():
    import json

    adapter = CodexAdapter()
    engine_event = adapter.engine_event(adapter.adapt_event(PAYLOAD))
    frame = json.loads(hook_http.build_request(engine_event, hook_http.DEFAULT_OP_NAME))
    assert frame["method"] == hook_http.DEFAULT_OP_NAME
    assert frame["params"]["payload"]["tool_input"] == {"command": "git push origin main"}
    assert frame["params"]["payload"]["session_id"] == "codex-sess-1"
    assert frame["_caller"]["session_id"] == "codex-sess-1"
    assert frame["_origin_worktree"] == "/work/repo"


def test_unserved_event_has_no_engine_event_and_renders_neutral():
    adapter = CodexAdapter()
    ev = adapter.adapt_event({**PAYLOAD, "hook_event_name": "SessionStart"})
    assert adapter.engine_event(ev) is None
    assert adapter.render(ev, {"decision": "deny", "reason": "x"}) == neutral_fallback("codex") == {}


def test_deny_renders_exactly_the_hook_http_deny_shape():
    adapter = CodexAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    out = adapter.render(ev, {"decision": "deny", "reason": "push to non-owned remote"})
    assert out == hook_http.deny_response("PreToolUse", "push to non-owned remote")


def test_nested_engine_deny_is_rendered_as_deny():
    adapter = CodexAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    engine = hook_http.deny_response("PreToolUse", "nested reason")
    assert adapter.render(ev, engine) == engine


def test_deny_without_reason_still_carries_a_reason():
    out = CodexAdapter().render(CodexAdapter().adapt_event(PAYLOAD), {"decision": "deny"})
    assert out["hookSpecificOutput"]["permissionDecisionReason"]


def test_allow_empty_and_ask_all_render_empty_object():
    adapter = CodexAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    for verdict in ({}, {"decision": "allow"}, {"decision": "ask", "reason": "r"}, None):
        assert adapter.render(ev, verdict) == {}


def test_verdict_parts_flat_nested_and_absent():
    assert verdict_parts({"permissionDecision": "deny", "permissionDecisionReason": "r"}) == ("deny", "r")
    assert verdict_parts({"decision": "deny", "reason": "r"}) == ("deny", "r")
    assert verdict_parts(None) == ("", "")
