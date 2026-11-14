"""agy adapter: protojson payload mapping, engine-event shape, verdict rendering, unserved events."""
from __future__ import annotations

import pytest

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


def _edit_call(name, args):
    return {**PAYLOAD, "toolCall": {"name": name, "args": args}}


def test_edit_tools_map_to_write_guard_names_with_file_path():
    expected = {
        "write_file": "Write", "write_to_file": "Write", "edit_file": "Edit",
        "replace_file_content": "Edit", "sed_file": "Edit", "multi_replace_file_content": "MultiEdit",
        "notebook_edit": "NotebookEdit",
    }
    for name, tool in expected.items():
        ev = AgyAdapter().adapt_event(_edit_call(name, {"TargetFile": "/work/repo/a.py"}))
        assert (ev.tool_name, ev.tool_input["file_path"]) == (tool, "/work/repo/a.py")


def test_edit_without_a_path_arg_yields_empty_file_path_and_view_file_stays_unmapped():
    ev = AgyAdapter().adapt_event(_edit_call("edit_file", {}))
    assert ev.tool_input["file_path"] == ""
    assert AgyAdapter().adapt_event(_edit_call("view_file", {"AbsolutePath": "/a"})).tool_name == "view_file"


def test_path_key_is_matched_case_insensitively_so_write_file_with_Path_gets_file_path():
    for name in ("write_file", "edit_file"):
        for key in ("Path", "path", "TARGETFILE"):
            ev = AgyAdapter().adapt_event(_edit_call(name, {key: "/work/repo/a.py", "Content": "x"}))
            assert ev.tool_input["file_path"] == "/work/repo/a.py"
    ev = AgyAdapter().adapt_event(_edit_call("write_file", {"Path": "", "Content": "x"}))
    assert ev.tool_input["file_path"] == ""
    ev = AgyAdapter().adapt_event(_edit_call("write_file", {"Other": "/work/repo/a.py"}))
    assert ev.tool_input["file_path"] == ""


@pytest.mark.parametrize("path_key", ["TargetFile", "Path"])
def test_agy_edit_file_event_reaches_write_guards_engine(monkeypatch, path_key):
    from coordinator_core.host_hooks import tool_policy
    from coordinator_core.write_guards import engine

    seen = []
    monkeypatch.setattr(engine, "evaluate", lambda event, **kw: seen.append(event) or {})
    adapter = AgyAdapter()
    engine_event = adapter.engine_event(adapter.adapt_event(_edit_call("edit_file", {path_key: "/work/repo/a.py"})))
    tool_policy.evaluate(engine_event)
    assert len(seen) == 1
    assert (seen[0]["tool_name"], seen[0]["tool_input"]["file_path"]) == ("Edit", "/work/repo/a.py")


def test_missing_workspace_and_session_are_empty():
    ev = AgyAdapter().adapt_event({"toolCall": {"name": "run_command", "args": {}}})
    assert (ev.cwd, ev.session_id) == ("", "")


def test_top_level_cwd_is_the_fallback_and_reaches_origin_worktree():
    import json

    adapter = AgyAdapter()
    cwd = "/work/agy-cwd"
    payload = {"toolCall": PAYLOAD["toolCall"], "cwd": cwd}
    engine_event = adapter.engine_event(adapter.adapt_event(payload))
    assert engine_event["cwd"] == cwd
    frame = json.loads(hook_http.build_request(engine_event, hook_http.DEFAULT_OP_NAME))
    assert frame["_origin_worktree"] == cwd
    # workspacePaths still wins when both are present
    assert adapter.adapt_event({**PAYLOAD, "cwd": "/other"}).cwd == "/work/repo"


def test_event_resolution_explicit_then_inferred():
    adapter = AgyAdapter()
    assert adapter.adapt_event(PAYLOAD, event="PostToolUse").event == "PostToolUse"
    assert adapter.adapt_event({**PAYLOAD, "hookEventName": "PostToolUse"}).event == "PostToolUse"
    assert adapter.adapt_event({**PAYLOAD, "error": "exit status 1"}).event == ""
    assert adapter.adapt_event({"conversationId": "c", "terminationReason": "NO_TOOL_CALL"}).event == ""


def test_subagent_payload_carries_agent_id_into_engine_event_and_em_payload_does_not():
    adapter = AgyAdapter()
    for key in ("agentId", "agent_id"):
        sub = adapter.engine_event(adapter.adapt_event({**PAYLOAD, key: "sub-1"}))
        assert sub["agent_id"] == "sub-1"
    for em_extra in ({}, {"agentId": ""}, {"agentId": None}):
        assert "agent_id" not in adapter.engine_event(adapter.adapt_event({**PAYLOAD, **em_extra}))


def test_subagent_payload_reaches_the_guard_chain_with_agent_id(monkeypatch):
    from coordinator_core.host_hooks import tool_policy

    seen = []
    monkeypatch.setattr(tool_policy, "evaluate", lambda event, **kw: seen.append(event) or {})
    from coordinator_core.host_hooks import shim

    adapter = AgyAdapter()
    host_event = adapter.adapt_event({**PAYLOAD, "agentId": "sub-1"})
    shim._cold_verdict(host_event, adapter.engine_event(host_event))
    assert seen and seen[0]["agent_id"] == "sub-1"


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


def test_allow_with_additional_context_renders_ask_carrying_the_text_never_allow():
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    nested = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "heads up"}}
    for verdict in ({"decision": "allow", "additionalContext": "heads up"}, nested,
                    {"additionalContext": "heads up"}):
        assert adapter.render(ev, verdict) == {"decision": "ask", "reason": "heads up"}
    assert adapter.render(ev, {"decision": "allow", "additionalContext": "  "}) == {"decision": "allow"}
    deny = adapter.render(ev, {"decision": "deny", "reason": "no", "additionalContext": "heads up"})
    assert deny == {"decision": "deny", "reason": "no"}


def test_ask_is_passed_through():
    adapter = AgyAdapter()
    out = adapter.render(adapter.adapt_event(PAYLOAD), {"decision": "ask", "reason": "confirm"})
    assert out == {"decision": "ask", "reason": "confirm"}
