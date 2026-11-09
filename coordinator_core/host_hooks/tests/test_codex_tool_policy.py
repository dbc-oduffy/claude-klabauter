"""Pure native-tool projections; no resident engine, subprocess or fleet writes."""
from coordinator_core.host_hooks.codex_adapter import CodexAdapter
from coordinator_core.host_hooks.codex_tools import patch_events
from coordinator_core.host_hooks import backstop, tool_policy
from coordinator_core.host_hooks.contract import HostEvent


def _patch(text, cwd=""):
    return {"tool_name": "apply_patch", "tool_input": {"command": text}, "cwd": cwd}


def test_add_projects_entire_body_and_native_marker(tmp_path):
    events, warnings = patch_events(_patch("*** Begin Patch\n*** Add File: a.txt\n+hello\n+world\n*** End Patch", str(tmp_path)))
    assert not warnings
    assert events[0]["tool_input"] == {"file_path": str(tmp_path / "a.txt"), "content": "hello\nworld\n"}
    assert events[0]["origin_tool_name"] == "apply_patch"


def test_unique_update_and_move_inspect_both_paths(tmp_path):
    (tmp_path / "a.txt").write_text("before\nkeep\n", encoding="utf-8")
    events, warnings = patch_events(_patch("*** Begin Patch\n*** Update File: a.txt\n*** Move to: b.txt\n@@\n-before\n+after\n keep\n*** End Patch", str(tmp_path)))
    assert not warnings
    assert events[0]["tool_input"] == {"file_path": str(tmp_path / "a.txt")}
    assert events[1]["tool_input"] == {"file_path": str(tmp_path / "b.txt"), "content": "after\nkeep\n"}


def test_ambiguous_patch_checks_path_without_fabricating_body(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("same\nsame\n", encoding="utf-8")
    events, warnings = patch_events(_patch("*** Begin Patch\n*** Update File: CLAUDE.md\n@@\n-same\n+new\n*** End Patch", str(tmp_path)))
    assert events[0]["tool_input"] == {"file_path": str(tmp_path / "CLAUDE.md")}
    assert warnings


def test_native_patch_command_never_reaches_shell_backstop(monkeypatch):
    def unexpected(_):
        raise AssertionError("patch is not shell input")
    monkeypatch.setattr(backstop, "_command", unexpected)
    assert backstop.match(HostEvent("codex", "PreToolUse", "s", "apply_patch", {"command": "git push"})) is None


def test_codex_context_survives_without_permission_override():
    adapter = CodexAdapter()
    event = adapter.adapt_event({"hook_event_name": "PreToolUse", "tool_name": "apply_patch"})
    assert adapter.engine_event(event)["host"] == "codex"
    result = adapter.render(event, {"permissionDecision": "allow", "additionalContext": "review this"})
    assert result == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "review this"}}


def test_native_child_metadata_is_preserved_without_fabrication():
    adapter = CodexAdapter()
    payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "session_id": "parent",
               "agent_id": "actual-child", "agent_type": "worker", "transcript_path": "child.jsonl"}
    event = adapter.engine_event(adapter.adapt_event(payload))
    assert event["session_id"] == "parent"
    assert event["agent_id"] == "actual-child"
    assert event["transcript_path"] == "child.jsonl"
    assert "agent_id" not in adapter.engine_event(adapter.adapt_event({"hook_event_name": "PreToolUse", "session_id": "parent"}))


def test_native_patch_routes_to_shared_write_engine(tmp_path, monkeypatch):
    from coordinator_core.write_guards import engine
    calls = []
    def evaluate(payload, **kwargs):
        calls.append((payload, kwargs))
        return {"hookSpecificOutput": {"permissionDecision": "allow", "additionalContext": "shared finding"}}
    monkeypatch.setattr(engine, "evaluate", evaluate)
    out = tool_policy.evaluate(_patch("*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch", str(tmp_path)))
    assert out == {"additionalContext": "shared finding"}
    assert calls[0][0]["tool_name"] == "Write"
    assert calls[0][1]["aggregate"] is True


def test_merge_preserves_real_deny_and_advisories():
    assert tool_policy._merge([{"hookSpecificOutput": {"permissionDecision": "allow", "additionalContext": "warn"}}], []) == {"additionalContext": "warn"}
    assert tool_policy._merge([[{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "floor"}}]], ["warn"])["permissionDecision"] == "deny"


def test_named_mcp_floor_does_not_scan_arbitrary_argument_text(monkeypatch):
    from coordinator_core.bash_guards import block_perforce_submit as p4
    monkeypatch.setattr(p4, "_armed", lambda: True)
    assert p4.check_mcp({"tool_name": "mcp__example_game_repo__source_control_submit", "tool_input": {}})
    assert p4.check_mcp({"tool_name": "mcp__example_game_repo__source_control_status", "tool_input": {"description": "submit changelist"}}) is None
    monkeypatch.setattr(p4, "_armed", lambda: False)
    assert p4.check_mcp({"tool_name": "mcp__example_game_repo__source_control_submit"}) is None
