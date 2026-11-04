"""agy adapter: protojson payload mapping, engine-event shape, verdict rendering, unserved events."""
from __future__ import annotations

from coordinator_core.host_hooks.agy_adapter import SERVED_EVENTS, AgyAdapter
from coordinator_core.host_hooks.contract import Adapter, neutral_fallback
from coordinator_core.warm import hook_http

PAYLOAD = {
    "conversationId": "1efd6565-4b6a-434a-989d-f7814c28bdd8",
    "workspacePaths": ["/work/repo", "/work/other"],
    "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
    "stepIdx": 3,
}


def test_conforms_to_adapter_protocol():
    assert isinstance(AgyAdapter(), Adapter)


def test_adapt_event_maps_run_command_to_bash_with_session_from_payload(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "env-conv")
    ev = AgyAdapter().adapt_event(PAYLOAD)
    assert ev.session_id == "1efd6565-4b6a-434a-989d-f7814c28bdd8"
    assert (ev.host, ev.event, ev.tool_name, ev.cwd) == ("agy", "PreToolUse", "Bash", "/work/repo")
    assert ev.tool_input == {"command": "git push origin main"}


def test_non_shell_tool_passes_args_through():
    ev = AgyAdapter().adapt_event({**PAYLOAD, "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/a"}}})
    assert (ev.tool_name, ev.tool_input) == ("view_file", {"AbsolutePath": "/a"})


def test_missing_workspace_and_session_are_empty():
    ev = AgyAdapter().adapt_event({"toolCall": {"name": "run_command", "args": {}}})
    assert (ev.cwd, ev.session_id) == ("", "")


def test_event_resolution_explicit_then_inferred():
    adapter = AgyAdapter()
    assert adapter.adapt_event(PAYLOAD, event="PostToolUse").event == "PostToolUse"
    assert adapter.adapt_event({**PAYLOAD, "hookEventName": "PostToolUse"}).event == "PostToolUse"
    assert adapter.adapt_event({**PAYLOAD, "error": "exit status 1"}).event == ""
    assert adapter.adapt_event({"conversationId": "c", "terminationReason": "NO_TOOL_CALL"}).event == ""


def test_served_events_match_hook_http():
    assert SERVED_EVENTS == hook_http.SERVED_EVENTS


def test_engine_event_is_framed_by_build_request_with_payload_session():
    import json

    adapter = AgyAdapter()
    engine_event = adapter.engine_event(adapter.adapt_event(PAYLOAD))
    frame = json.loads(hook_http.build_request(engine_event, hook_http.DEFAULT_OP_NAME))
    assert frame["params"]["payload"]["tool_name"] == "Bash"
    assert frame["params"]["payload"]["tool_input"] == {"command": "git push origin main"}
    assert frame["_caller"]["session_id"] == "1efd6565-4b6a-434a-989d-f7814c28bdd8"
    assert frame["_origin_worktree"] == "/work/repo"


def test_unserved_event_has_no_engine_event_and_renders_ask_fallback():
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD, event="SessionStart")
    assert adapter.engine_event(ev) is None
    assert adapter.render(ev, {"decision": "deny", "reason": "x"}) == neutral_fallback("agy")


def test_deny_renders_agy_deny_with_reason():
    adapter = AgyAdapter()
    out = adapter.render(adapter.adapt_event(PAYLOAD), {"decision": "deny", "reason": "irreversible"})
    assert out == {"decision": "deny", "reason": "irreversible"}


def test_nested_claude_shaped_deny_is_rendered_as_agy_deny():
    adapter = AgyAdapter()
    out = adapter.render(adapter.adapt_event(PAYLOAD), hook_http.deny_response("PreToolUse", "nested"))
    assert out == {"decision": "deny", "reason": "nested"}


def test_allow_and_empty_verdict_render_explicit_allow_never_empty_object():
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    for verdict in ({}, {"decision": "allow"}):
        out = adapter.render(ev, verdict)
        assert out == {"decision": "allow"}


def test_ask_is_passed_through():
    adapter = AgyAdapter()
    out = adapter.render(adapter.adapt_event(PAYLOAD), {"decision": "ask", "reason": "confirm"})
    assert out == {"decision": "ask", "reason": "confirm"}
