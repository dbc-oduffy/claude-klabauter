"""Pins the three properties that make this a HOOK transport rather than a GUARD transport.

Before these, `warm/hook_http.py` + `warm/supervisor.py` moved a PreToolUse verdict and
nothing else: the op name was hardcoded, the payload carried only the guard's own fields,
and every non-deny response was `{"hookSpecificOutput": {"hookEventName": ...}}`. Twenty of
the twenty-seven live registrations are not deny-capable and six of them exist ONLY to
inject content, so on that transport they would have returned 200 and done nothing --
at any listener availability, with no availability sampling able to see it.

Each test below fails loudly if one of those three regressions returns. The injection test
in particular is the one with no natural symptom: nothing errors, nothing times out, the
op runs and the round trip succeeds -- the content is simply dropped one layer above the op.
"""

from __future__ import annotations

import json

from coordinator_core.warm import hook_http


def test_routing_is_per_registration_not_per_event():
    """Three SessionStart registrations name three different scripts; `hook_event_name`
    cannot tell them apart, so the route has to come from the URL."""
    assert hook_http.op_for_path("/hook/session.boot_sweep") == "session.boot_sweep"
    assert hook_http.op_for_path("/hook/hooks.track_dispatched_agents") == "hooks.track_dispatched_agents"
    assert (
        hook_http.op_for_path("/hook/hooks.track_dispatched_agents")
        != hook_http.op_for_path("/hook/session.boot_sweep")
    )


def test_bare_hook_path_still_routes_to_the_guard_op():
    """The arms in budget-manifest.json measured a bare `/hook` POST; keeping it identical
    is what lets those figures stay quotable after routing landed."""
    assert hook_http.op_for_path("/hook") == hook_http.DEFAULT_OP_NAME
    assert hook_http.op_for_path("/hook/") == hook_http.DEFAULT_OP_NAME


def test_unroutable_paths_are_refused_rather_than_dispatched():
    """A registration is a string in a config file a plugin update can rewrite. Out-of-
    namespace, traversal, and nested paths must resolve to no op at all."""
    for path in ("/hook/ceremony.scoped_git_commit", "/hook/../etc", "/hook/hooks.a/b", "/hooks", "/hook/"[:5] + "x"):
        assert hook_http.op_for_path(path) is None or path.rstrip("/") == "/hook"


def test_per_event_fields_reach_the_op():
    """`prompt`, `source`, `tool_response`, `trigger`, `agent_type` -- an op could not
    previously distinguish these being absent from being empty."""
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
    """Forwarding per-event fields must not have widened the env boundary with them."""
    payload = hook_http.payload_from_event(
        {"hook_event_name": "PreToolUse", "env": {"COORDINATOR_ALLOW_X": "1", "AWS_SECRET_ACCESS_KEY": "leak"}}
    )
    assert payload["env"] == {"COORDINATOR_ALLOW_X": "1"}
    assert "AWS_SECRET_ACCESS_KEY" not in json.dumps(payload)


def test_injected_content_survives_the_success_path():
    """The regression with no symptom: op ran, 200 returned, content dropped."""
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"additionalContext": "ctx for the model", "systemMessage": "for the operator"}}
    ).encode("utf-8")
    body = hook_http.interpret_result("UserPromptExpansion", frame)
    assert body["additionalContext"] == "ctx for the model"
    assert body["systemMessage"] == "for the operator"
    assert body["hookSpecificOutput"]["hookEventName"] == "UserPromptExpansion"


def test_success_path_still_refuses_to_fabricate_an_allow():
    """Carrying injected content must not have turned the no-objection verdict into an
    explicit `allow`, which would override the operator's own permission settings."""
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
    assert "did not run" in body["additionalContext"]
    assert "guard did not run" in body["systemMessage"]


def test_bare_hook_still_refuses_an_event_it_has_no_route_for():
    """The two designs compose rather than compete. A bare `/hook` POST carries no route,
    so `route_for_event` decides -- and for a non-served event that is a loud did-not-run,
    not a guard verdict on a question the event never posed. An explicit `/hook/<op>`
    bypasses the inference entirely, because the registration already named the op."""
    assert hook_http.route_for_event("PreToolUse") == hook_http.DEFAULT_OP_NAME
    assert hook_http.route_for_event("SessionStart") is None

    body = hook_http.unserved_response("SessionStart")
    assert "did not run" in body["additionalContext"]
    assert "permissionDecision" not in json.dumps(body)

    assert hook_http.op_for_path("/hook/session.boot_sweep") == "session.boot_sweep"
