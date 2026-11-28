"""agy adapter: protojson payload mapping, engine-event shape, verdict rendering, unserved events."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.host_hooks import agy_adapter
from coordinator_core.host_hooks.agy_adapter import SERVED_EVENTS, AgyAdapter, advisory_dir, stash_advisory
from coordinator_core.host_hooks.contract import Adapter, neutral_fallback
from coordinator_core.warm import hook_http

_FIXTURE = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "tests" / "fixtures" / "host-real-tool-names.json"

PAYLOAD = {
    "conversationId": "1efd6565-4b6a-434a-989d-f7814c28bdd8",
    "workspacePaths": ["/work/repo", "/work/other"],
    "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
    "stepIdx": 3,
}


def test_conforms_to_adapter_protocol() -> None:
    assert isinstance(AgyAdapter(), Adapter)


def test_adapt_event_maps_run_command_to_bash_with_session_from_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "env-conv")
    ev = AgyAdapter().adapt_event(PAYLOAD)
    assert ev.session_id == "1efd6565-4b6a-434a-989d-f7814c28bdd8"
    assert (ev.host, ev.event, ev.tool_name, ev.cwd) == ("agy", "PreToolUse", "Bash", "/work/repo")
    assert ev.tool_input == {"command": "git push origin main"}


def test_non_shell_tool_passes_args_through() -> None:
    ev = AgyAdapter().adapt_event({**PAYLOAD, "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/a"}}})
    assert (ev.tool_name, ev.tool_input) == ("view_file", {"AbsolutePath": "/a"})


def _edit_call(name, args):
    return {**PAYLOAD, "toolCall": {"name": name, "args": args}}


def test_edit_tools_map_to_write_guard_names_with_file_path() -> None:
    expected = {
        "write_file": "Write", "write_to_file": "Write", "edit_file": "Edit",
        "replace_file_content": "Edit", "sed_file": "Edit", "multi_replace_file_content": "MultiEdit",
        "notebook_edit": "NotebookEdit",
    }
    for name, tool in expected.items():
        ev = AgyAdapter().adapt_event(_edit_call(name, {"TargetFile": "/work/repo/a.py"}))
        assert (ev.tool_name, ev.tool_input["file_path"]) == (tool, "/work/repo/a.py")


_AGY_REAL = json.loads(_FIXTURE.read_text(encoding="utf-8"))["agy"]


@pytest.mark.parametrize("entry", [e for e in _AGY_REAL if e["kind"] == "shell"], ids=lambda e: e["name"])
def test_real_agy_shell_tool_maps_to_bash(entry) -> None:
    ev = AgyAdapter().adapt_event(_edit_call(entry["name"], {"CommandLine": "git push origin main"}))
    assert (ev.tool_name, ev.tool_input["command"]) == ("Bash", "git push origin main")


@pytest.mark.parametrize("entry", [e for e in _AGY_REAL if e["kind"] in ("edit", "write")], ids=lambda e: e["name"])
def test_real_agy_edit_or_write_tool_maps_to_a_write_guard_tool_with_file_path(entry) -> None:
    ev = AgyAdapter().adapt_event(_edit_call(entry["name"], {"Path": "/work/repo/a.py"}))
    assert ev.tool_name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    assert ev.tool_input["file_path"] == "/work/repo/a.py"


def test_edit_without_a_path_arg_yields_empty_file_path_and_view_file_stays_unmapped() -> None:
    ev = AgyAdapter().adapt_event(_edit_call("edit_file", {}))
    assert ev.tool_input["file_path"] == ""
    assert AgyAdapter().adapt_event(_edit_call("view_file", {"AbsolutePath": "/a"})).tool_name == "view_file"


def test_path_key_is_matched_case_insensitively_so_write_file_with_Path_gets_file_path() -> None:
    for name in ("write_file", "edit_file"):
        for key in ("Path", "path", "TARGETFILE"):
            ev = AgyAdapter().adapt_event(_edit_call(name, {key: "/work/repo/a.py", "Content": "x"}))
            assert ev.tool_input["file_path"] == "/work/repo/a.py"
    ev = AgyAdapter().adapt_event(_edit_call("write_file", {"Path": "", "Content": "x"}))
    assert ev.tool_input["file_path"] == ""
    ev = AgyAdapter().adapt_event(_edit_call("write_file", {"Other": "/work/repo/a.py"}))
    assert ev.tool_input["file_path"] == ""


def test_write_file_Content_reaches_the_guards_as_lowercase_content_and_edit_file_gets_none() -> None:
    for name in ("write_file", "write_to_file"):
        ev = AgyAdapter().adapt_event(_edit_call(name, {"Path": "/work/repo/a.py", "Content": "body"}))
        assert ev.tool_input["content"] == "body"
    ev = AgyAdapter().adapt_event(_edit_call("edit_file", {"Path": "/work/repo/a.py", "Content": "body"}))
    assert "content" not in ev.tool_input


@pytest.mark.parametrize("path_key", ["TargetFile", "Path"])
def test_agy_edit_file_event_reaches_write_guards_engine(monkeypatch: pytest.MonkeyPatch, path_key) -> None:
    from coordinator_core.host_hooks import tool_policy
    from coordinator_core.write_guards import engine

    seen = []
    monkeypatch.setattr(engine, "evaluate", lambda event, **kw: seen.append(event) or {})
    adapter = AgyAdapter()
    engine_event = adapter.engine_event(adapter.adapt_event(_edit_call("edit_file", {path_key: "/work/repo/a.py"})))
    tool_policy.evaluate(engine_event)
    assert len(seen) == 1
    assert (seen[0]["tool_name"], seen[0]["tool_input"]["file_path"]) == ("Edit", "/work/repo/a.py")


def test_missing_workspace_and_session_are_empty() -> None:
    ev = AgyAdapter().adapt_event({"toolCall": {"name": "run_command", "args": {}}})
    assert (ev.cwd, ev.session_id) == ("", "")


def test_top_level_cwd_is_the_fallback_and_reaches_origin_worktree() -> None:
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


def test_event_resolution_explicit_then_inferred() -> None:
    adapter = AgyAdapter()
    assert adapter.adapt_event(PAYLOAD, event="PostToolUse").event == "PostToolUse"
    assert adapter.adapt_event({**PAYLOAD, "hookEventName": "PostToolUse"}).event == "PostToolUse"
    assert adapter.adapt_event({**PAYLOAD, "error": "exit status 1"}).event == ""
    assert adapter.adapt_event({"conversationId": "c", "terminationReason": "NO_TOOL_CALL"}).event == ""


def test_subagent_payload_carries_agent_id_into_engine_event_and_em_payload_does_not() -> None:
    adapter = AgyAdapter()
    for key in ("agentId", "agent_id"):
        sub = adapter.engine_event(adapter.adapt_event({**PAYLOAD, key: "sub-1"}))
        assert sub["agent_id"] == "sub-1"
    for em_extra in ({}, {"agentId": ""}, {"agentId": None}):
        assert "agent_id" not in adapter.engine_event(adapter.adapt_event({**PAYLOAD, **em_extra}))


def test_subagent_payload_reaches_the_guard_chain_with_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.host_hooks import cold_chain

    seen = []
    monkeypatch.setattr(cold_chain, "evaluate", lambda event: seen.append(event) or {})
    from coordinator_core.host_hooks import shim

    adapter = AgyAdapter()
    host_event = adapter.adapt_event({**PAYLOAD, "agentId": "sub-1"})
    shim._cold_verdict(host_event, adapter.engine_event(host_event))
    assert seen and seen[0]["agent_id"] == "sub-1"


def test_served_events_match_hook_http() -> None:
    assert SERVED_EVENTS == hook_http.SERVED_EVENTS


def test_engine_event_is_framed_by_build_request_with_payload_session() -> None:
    import json

    adapter = AgyAdapter()
    engine_event = adapter.engine_event(adapter.adapt_event(PAYLOAD))
    frame = json.loads(hook_http.build_request(engine_event, hook_http.DEFAULT_OP_NAME))
    assert frame["params"]["payload"]["tool_name"] == "Bash"
    assert frame["params"]["payload"]["tool_input"] == {"command": "git push origin main"}
    assert frame["_caller"]["session_id"] == "1efd6565-4b6a-434a-989d-f7814c28bdd8"
    assert frame["_origin_worktree"] == "/work/repo"


def test_unserved_event_has_no_engine_event_and_renders_ask_fallback() -> None:
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD, event="SessionStart")
    assert adapter.engine_event(ev) is None
    assert adapter.render(ev, {"decision": "deny", "reason": "x"}) == neutral_fallback("agy")


def test_deny_renders_agy_deny_with_reason() -> None:
    adapter = AgyAdapter()
    out = adapter.render(adapter.adapt_event(PAYLOAD), {"decision": "deny", "reason": "irreversible"})
    assert out == {"decision": "deny", "reason": "irreversible"}


def test_nested_claude_shaped_deny_is_rendered_as_agy_deny() -> None:
    adapter = AgyAdapter()
    out = adapter.render(adapter.adapt_event(PAYLOAD), hook_http.deny_response("PreToolUse", "nested"))
    assert out == {"decision": "deny", "reason": "nested"}


def test_allow_and_empty_verdict_render_explicit_allow_never_empty_object() -> None:
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    for verdict in ({}, {"decision": "allow"}):
        out = adapter.render(ev, verdict)
        assert out == {"decision": "allow"}


@pytest.fixture(autouse=True)
def _settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


def _stashed(conversation: str = PAYLOAD["conversationId"]) -> list:
    folder = advisory_dir() / conversation
    return [p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.txt"))] if folder.is_dir() else []


def _visible(text: str) -> dict:
    # agy documents `injectSteps` on SessionStart and PreInvocation only (hooks.md sections 1, 4; C0 evidence in
    # docs/plans/2026-11-16-hnd-agy-headless-launch-and-nag-ch-987fbb.evidence.yaml), and `-p` auto-approves
    # an `ask`: the PreToolUse answer is an allow, and the text waits in the stash for the next PreInvocation.
    return {"decision": "allow", "reason": text}


def test_a_pretool_answer_never_carries_injectsteps_and_stashes_the_text_for_preinvocation() -> None:
    adapter = AgyAdapter()
    out = adapter.render(adapter.adapt_event(PAYLOAD), {"decision": "ask", "reason": "heads up"})
    assert "injectSteps" not in out and out == _visible("heads up")
    assert _stashed() == ["heads up"]


def test_an_advisory_for_an_unusable_conversation_id_is_not_stashed_but_still_allows(tmp_path: Path) -> None:
    adapter = AgyAdapter()
    ev = adapter.adapt_event({**PAYLOAD, "conversationId": "../escape"})
    assert adapter.render(ev, {"decision": "ask", "reason": "x"}) == _visible("x")
    assert not stash_advisory("", "x") and not (tmp_path / "unimatrix" / "agy-advisories").exists()


def test_a_deny_stashes_nothing() -> None:
    adapter = AgyAdapter()
    adapter.render(adapter.adapt_event(PAYLOAD), {"decision": "deny", "reason": "no"})
    assert _stashed() == []


def test_allow_with_additional_context_renders_a_model_visible_allow_never_ask() -> None:
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    nested = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "heads up"}}
    for verdict in ({"decision": "allow", "additionalContext": "heads up"}, nested,
                    {"additionalContext": "heads up"}):
        assert adapter.render(ev, verdict) == _visible("heads up")
    assert adapter.render(ev, {"decision": "allow", "additionalContext": "  "}) == {"decision": "allow"}
    deny = adapter.render(ev, {"decision": "deny", "reason": "no", "additionalContext": "heads up"})
    assert deny == {"decision": "deny", "reason": "no"}


@pytest.mark.parametrize("decision", ["ask", "force_ask"])
def test_ask_without_a_registry_class_is_a_model_visible_allow_never_ask(decision) -> None:
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    assert adapter.render(ev, {"decision": decision, "reason": "confirm"}) == _visible("confirm")
    assert adapter.render(ev, {"decision": decision}) == _visible("coordinator: confirm")
    nested = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                     "permissionDecisionReason": "confirm"}}
    assert adapter.render(ev, nested) == _visible("confirm")


@pytest.mark.parametrize("decision", ["ask", "force_ask"])
def test_ask_from_a_registry_classed_guard_is_a_deny_with_reason(decision) -> None:
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    out = adapter.render(ev, {"decision": decision, "reason": "pushes to a non-owned remote",
                              "guard": "block-push-pr-non-owned-remote"})
    assert out == {"decision": "deny", "reason": "pushes to a non-owned remote"}


def test_ask_naming_an_unregistered_guard_stays_an_advisory_never_a_deny() -> None:
    adapter = AgyAdapter()
    ev = adapter.adapt_event(PAYLOAD)
    out = adapter.render(ev, {"decision": "ask", "reason": "confirm", "guard": "bump-foreign-repo-write"})
    assert out == _visible("confirm")


def _bad_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conversation: str) -> None:
    folder = tmp_path / "agy-conversations"
    folder.mkdir()
    (folder / f"{conversation}.db").write_text("not sqlite", encoding="utf-8")
    monkeypatch.setattr(agy_adapter, "conversations_dir", lambda: folder)


def test_a_read_failure_answers_em_and_records_one_line_naming_conversation_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation = "1efd6565-4b6a-434a-989d-f7814c28bdd8"
    _bad_store(tmp_path, monkeypatch, conversation)
    assert agy_adapter.is_subagent_conversation(conversation) is False
    lines = agy_adapter.read_failure_log().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["conversation"] == conversation
    assert record["error"].startswith("DatabaseError")
    assert record["at"]


def test_a_failing_record_path_never_raises_and_the_answer_stays_em(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation = "1efd6565-4b6a-434a-989d-f7814c28bdd8"
    _bad_store(tmp_path, monkeypatch, conversation)
    (tmp_path / "unimatrix").write_text("a file where the log directory must go", encoding="utf-8")
    assert agy_adapter.is_subagent_conversation(conversation) is False


def test_a_readable_or_absent_store_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy_adapter, "conversations_dir", lambda: tmp_path / "absent")
    assert agy_adapter.is_subagent_conversation("1efd6565-4b6a-434a-989d-f7814c28bdd8") is False
    assert not agy_adapter.read_failure_log().exists()
