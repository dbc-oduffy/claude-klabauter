
from __future__ import annotations

import json

from coordinator_core.warm import hook_http


def test_routing_is_per_registration_not_per_event():
    assert hook_http.op_for_path("/hook/session.boot_sweep") == "session.boot_sweep"
    assert hook_http.op_for_path("/hook/hooks.track_dispatched_agents") == "hooks.track_dispatched_agents"
    assert (
        hook_http.op_for_path("/hook/hooks.track_dispatched_agents")
        != hook_http.op_for_path("/hook/session.boot_sweep")
    )


def test_bare_hook_path_still_routes_to_the_guard_op():
    assert hook_http.op_for_path("/hook") == hook_http.DEFAULT_OP_NAME
    assert hook_http.op_for_path("/hook/") == hook_http.DEFAULT_OP_NAME


def test_unroutable_paths_are_refused_rather_than_dispatched():
    for path in ("/hook/ceremony.scoped_git_commit", "/hook/../etc", "/hook/hooks.a/b", "/hooks", "/hook/"[:5] + "x"):
        assert hook_http.op_for_path(path) is None


def test_per_event_fields_reach_the_op():
    payload = hook_http.payload_from_event(
        {
            "hook_event_name": "UserPromptExpansion",
            "prompt": "the operator's text",
            "source": "startup",
            "tool_response": {"ok": True},
            "trigger": "auto",
            "agent_type": "executor",
        }
    )
    assert payload["prompt"] == "the operator's text"
    assert payload["source"] == "startup"
    assert payload["tool_response"] == {"ok": True}
    assert payload["trigger"] == "auto"
    assert payload["agent_type"] == "executor"


def test_env_is_still_the_only_narrowed_key():
    payload = hook_http.payload_from_event(
        {"hook_event_name": "PreToolUse", "env": {"COORDINATOR_ALLOW_X": "1", "AWS_SECRET_ACCESS_KEY": "leak"}}
    )
    assert payload["env"] == {"COORDINATOR_ALLOW_X": "1"}
    assert "AWS_SECRET_ACCESS_KEY" not in json.dumps(payload)


def test_injected_content_survives_the_success_path():
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"additionalContext": "ctx for the model", "systemMessage": "for the operator"}}
    ).encode("utf-8")
    body = hook_http.interpret_result("UserPromptExpansion", frame)
    assert body["hookSpecificOutput"]["additionalContext"] == "ctx for the model"
    assert body["systemMessage"] == "for the operator"
    assert body["hookSpecificOutput"]["hookEventName"] == "UserPromptExpansion"


def test_success_path_still_refuses_to_fabricate_an_allow():
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"additionalContext": "ctx", "permissionDecision": "allow"}}
    ).encode("utf-8")
    body = hook_http.interpret_result("PreToolUse", frame)
    assert "permissionDecision" not in body["hookSpecificOutput"]
    assert "permissionDecision" not in json.dumps(body)


def test_a_deny_is_unaffected_by_the_passthrough():
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"permissionDecision": "deny", "permissionDecisionReason": "scoped pathspec missing"}}
    ).encode("utf-8")
    body = hook_http.interpret_result("PreToolUse", frame)
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert body["hookSpecificOutput"]["permissionDecisionReason"] == "scoped pathspec missing"


def test_an_error_envelope_is_still_not_a_verdict():
    """METHOD_NOT_FOUND for an op a clone predates must stay a loud did-not-run, not an
    injection-shaped success with empty content."""
    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}}).encode("utf-8")
    body = hook_http.interpret_result("SessionStart", frame)
    assert "did not run" in body["hookSpecificOutput"]["additionalContext"]
    assert "guard did not run" in body["systemMessage"]


def test_bare_hook_still_refuses_an_event_it_has_no_route_for():
    assert hook_http.route_for_event("PreToolUse") == hook_http.DEFAULT_OP_NAME
    assert hook_http.route_for_event("SessionStart") is None

    body = hook_http.unserved_response("SessionStart")
    assert "did not run" in body["hookSpecificOutput"]["additionalContext"]
    assert "permissionDecision" not in json.dumps(body)

    assert hook_http.op_for_path("/hook/session.boot_sweep") == "session.boot_sweep"


def _ctx(body):
    return body.get("hookSpecificOutput", {}).get("additionalContext")


def test_additional_context_is_nested_where_the_harness_reads_it():
    """MEASURED against harness 2.1.245 (claude-klabauter-0e): the top-level key is IGNORED
    and the nested one is honoured, discriminated by sending each shape alone. A top-level
    copy is not a harmless duplicate -- it is the injection silently going nowhere."""
    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"additionalContext": "sentinel"}}).encode("utf-8")
    body = hook_http.interpret_result("UserPromptSubmit", frame)
    assert _ctx(body) == "sentinel"
    assert "additionalContext" not in body


def test_an_op_that_already_nests_keeps_its_own_value():
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"additionalContext": "top", "hookSpecificOutput": {"additionalContext": "nested"}}}
    ).encode("utf-8")
    assert _ctx(hook_http.interpret_result("UserPromptSubmit", frame)) == "nested"


def test_the_did_not_run_warning_actually_reaches_the_model():
    for body in (
        hook_http.unreachable_response("PreToolUse", "listener down"),
        hook_http.unserved_response("SessionStart"),
    ):
        assert "did not run" in _ctx(body)
        assert "additionalContext" not in body
        assert body["systemMessage"]


def test_system_message_and_suppress_output_stay_top_level():
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"systemMessage": "for the operator", "suppressOutput": True}}
    ).encode("utf-8")
    body = hook_http.interpret_result("Stop", frame)
    assert body["systemMessage"] == "for the operator"
    assert body["suppressOutput"] is True


# tests pin the shape, not the enum: see `EVENTS_REJECTING_HOOK_SPECIFIC_OUTPUT`'s own


def test_sessionend_responses_omit_the_wrapper_the_harness_rejects():
    for body in (
        hook_http.allow_response("SessionEnd"),
        hook_http.unreachable_response("SessionEnd", "engine down"),
        hook_http.unserved_response("SessionEnd"),
    ):
        assert "hookSpecificOutput" not in body


def test_a_served_event_still_carries_the_wrapper_and_its_event_name():
    body = hook_http.allow_response("PostToolUse")
    assert body["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_sessionend_response_carries_no_context_key_at_all():
    for result in ({"additionalContext": "ctx"}, {"hookSpecificOutput": {"additionalContext": "ctx"}}):
        body = hook_http.allow_response("SessionEnd", result)
        assert "additionalContext" not in body
        assert "hookSpecificOutput" not in body


def test_a_deny_on_a_wrapper_refusing_event_reports_unrun_never_a_bare_deny():
    """The worst-direction failure this module exists to prevent, closed in code.

    `deny_response` bypasses `_envelope` because a deny IS the nested keys. On an event the
    harness refuses a wrapper for, emitting them fails validation -- and the harness fails
    open on a response it cannot read, so the one path whose whole job is to BLOCK would
    become a silent no-op exactly when it fires.

    Unreachable today: `BLOCKING_EVENTS` is `PreToolUse` alone and no op on a wrapper-
    refusing event emits a deny. It was documented as unreachable and not enforced, which is
    the same shape as a guard that reads correct and attests nothing. Mutation check: delete
    the `EVENTS_REJECTING_HOOK_SPECIFIC_OUTPUT` check in `_decision_to_response` and this
    fails with a `hookSpecificOutput` carrying `permissionDecision: deny`.
    """
    body = hook_http._decision_to_response(
        "SessionEnd", {"permissionDecision": "deny", "permissionDecisionReason": "nope"}
    )
    assert "hookSpecificOutput" not in body
    assert body.get("permissionDecision") is None
    assert "did not run" in body["systemMessage"]


def test_a_deny_on_a_normal_blocking_event_is_untouched():
    body = hook_http._decision_to_response(
        "PreToolUse", {"permissionDecision": "deny", "permissionDecisionReason": "nope"}
    )
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert body["hookSpecificOutput"]["permissionDecisionReason"] == "nope"
