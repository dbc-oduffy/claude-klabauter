"""grok adapter: edit/shell tool mapping, engine-event shape, verdict rendering, unserved events."""
from __future__ import annotations

import pytest

from coordinator_core.host_hooks.contract import Adapter, neutral_fallback
from coordinator_core.host_hooks.grok_adapter import SERVED_EVENTS, GrokAdapter
from coordinator_core.warm import hook_http


def _payload(name, args, **extra):
    return {"cwd": "/work/repo", "hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args, **extra}


def test_conforms_to_adapter_protocol():
    assert isinstance(GrokAdapter(), Adapter)


def test_served_events_match_hook_http():
    assert SERVED_EVENTS == hook_http.SERVED_EVENTS


@pytest.mark.parametrize("name,expected", [
    ("edit_file", "Edit"), ("Edit", "Edit"), ("MultiEdit", "MultiEdit"),
    ("write_file", "Write"), ("Write", "Write"), ("NotebookEdit", "NotebookEdit"),
])
def test_edit_tools_map_to_write_guard_names(name, expected):
    ev = GrokAdapter().adapt_event(_payload(name, {"file_path": "/a/b.py", "content": "x"}))
    assert ev.tool_name == expected
    assert ev.tool_input["file_path"] == "/a/b.py"
    assert ev.tool_input["content"] == "x"
    assert (ev.host, ev.event, ev.cwd) == ("grok", "PreToolUse", "/work/repo")


@pytest.mark.parametrize("key", ["file_path", "path", "Path"])
def test_path_read_from_each_contract_key(key):
    ev = GrokAdapter().adapt_event(_payload("write_file", {key: "/a/b.py"}))
    assert ev.tool_input["file_path"] == "/a/b.py"


def test_file_path_wins_over_path_and_empty_when_absent():
    ev = GrokAdapter().adapt_event(_payload("edit_file", {"path": "/p", "file_path": "/f"}))
    assert ev.tool_input["file_path"] == "/f"
    assert GrokAdapter().adapt_event(_payload("edit_file", {})).tool_input["file_path"] == ""


@pytest.mark.parametrize("name", ["Bash", "bash", "shell", "run_command", "run_shell_command"])
def test_shell_tools_map_to_bash_with_command(name):
    ev = GrokAdapter().adapt_event(_payload(name, {"command": "git push origin main"}))
    assert (ev.tool_name, ev.tool_input["command"]) == ("Bash", "git push origin main")


def test_shell_command_falls_back_to_cmd_key():
    ev = GrokAdapter().adapt_event(_payload("shell", {"cmd": "ls"}))
    assert ev.tool_input["command"] == "ls"


def test_unknown_tool_passes_through_unchanged():
    ev = GrokAdapter().adapt_event(_payload("view_file", {"path": "/a"}))
    assert (ev.tool_name, ev.tool_input) == ("view_file", {"path": "/a"})


def test_session_id_is_optional_and_taken_from_payload():
    assert GrokAdapter().adapt_event(_payload("Bash", {"command": "ls"})).session_id == ""
    assert GrokAdapter().adapt_event(_payload("Bash", {"command": "ls"}, session_id="g-1")).session_id == "g-1"


def test_engine_event_is_claude_shaped_for_edit_and_shell():
    a = GrokAdapter()
    edit = a.engine_event(a.adapt_event(_payload("edit_file", {"path": "/a/b.py"})))
    assert edit["tool_name"] == "Edit" and edit["tool_input"]["file_path"] == "/a/b.py"
    shell = a.engine_event(a.adapt_event(_payload("shell", {"command": "ls"})))
    assert shell["tool_name"] == "Bash" and shell["tool_input"]["command"] == "ls"
    assert shell["hook_event_name"] == "PreToolUse"


def test_engine_event_stamps_the_grok_host_for_edit_and_shell():
    a = GrokAdapter()
    for name, args in (("edit_file", {"path": "/a/b.py"}), ("shell", {"command": "ls"})):
        assert a.engine_event(a.adapt_event(_payload(name, args)))["host"] == "grok"


def test_unserved_event_has_no_engine_event_and_renders_neutral():
    a = GrokAdapter()
    ev = a.adapt_event({**_payload("Bash", {"command": "ls"}), "hook_event_name": "PostToolUse"})
    assert a.engine_event(ev) is None
    assert a.render(ev, {"permissionDecision": "deny"}) == neutral_fallback("grok") == {}


def test_render_deny_ask_and_default():
    a = GrokAdapter()
    ev = a.adapt_event(_payload("Bash", {"command": "ls"}))
    deny = a.render(ev, {"permissionDecision": "deny", "permissionDecisionReason": "no"})
    assert deny == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "no"}}
    nested = a.render(ev, {"hookSpecificOutput": {"permissionDecision": "ask", "permissionDecisionReason": "sure?"}})
    assert nested["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert a.render(ev, {"decision": "force_ask"})["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert a.render(ev, {}) == {}
    assert a.render(ev, {"permissionDecision": "allow"}) == {}


def test_render_allow_with_advisory_carries_additional_context_and_bare_allow_stays_neutral():
    a = GrokAdapter()
    ev = a.adapt_event(_payload("Bash", {"command": "ls"}))
    flat = a.render(ev, {"permissionDecision": "allow", "additionalContext": "nag"})
    assert flat == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                           "additionalContext": "nag"}}
    nested = a.render(ev, {"hookSpecificOutput": {"permissionDecision": "allow", "additionalContext": "nag",
                                                  "permissionDecisionReason": "why"}})
    assert nested["hookSpecificOutput"]["additionalContext"] == "nag"
    assert nested["hookSpecificOutput"]["permissionDecisionReason"] == "why"
    ask = a.render(ev, {"decision": "ask", "reason": "sure?", "additionalContext": "nag"})
    assert ask["hookSpecificOutput"]["additionalContext"] == "nag"
    assert a.render(ev, {"permissionDecision": "allow", "additionalContext": ""}) == {}
    assert a.render(ev, {"permissionDecision": "allow"}) == {}


_CONTRACT_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "edit_file", "write_file")


@pytest.mark.parametrize("tool", _CONTRACT_EDIT_TOOLS)
def test_every_pinned_edit_tool_yields_an_edit_event_with_file_path(tool):
    ev = GrokAdapter().adapt_event(_payload(tool, {"path": "/a/b.py"}))
    assert ev.tool_name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    assert ev.tool_input["file_path"] == "/a/b.py"


def test_file_path_key_outranks_path_keys_and_session_id_is_optional():
    ev = GrokAdapter().adapt_event(_payload("edit_file", {"file_path": "/first", "path": "/second", "Path": "/third"}))
    assert ev.tool_input["file_path"] == "/first"
    ev = GrokAdapter().adapt_event(_payload("edit_file", {"file_path": "/x"}, session_id="s1"))
    assert ev.tool_input["file_path"] == "/x"


def test_non_edit_unknown_tool_is_not_mapped_to_an_edit_name():
    ev = GrokAdapter().adapt_event(_payload("web_search", {"query": "q"}))
    assert ev.tool_name == "web_search"


def test_subagent_type_maps_to_agent_type_and_child_session_id_to_agent_id():
    ev = GrokAdapter().adapt_event(_payload("read_file", {"path": "/x"}, session_id="child-1", subagentType="explore"))
    assert ev.metadata["agent_type"] == "explore"
    assert ev.metadata["agent_id"] == "child-1"
    out = GrokAdapter().engine_event(ev)
    assert out["agent_id"] == "child-1" and out["agent_type"] == "explore"


def test_payload_without_subagent_type_stays_em_even_with_a_subagent_type_tool_arg():
    ev = GrokAdapter().adapt_event(
        _payload("spawn_subagent", {"subagent_type": "explore", "prompt": "p"}, session_id="em-1"))
    assert "agent_id" not in ev.metadata and "agent_type" not in ev.metadata


def test_subagent_type_without_session_id_does_not_invent_an_agent_id():
    ev = GrokAdapter().adapt_event(_payload("read_file", {"path": "/x"}, subagentType="explore"))
    assert "agent_id" not in ev.metadata
