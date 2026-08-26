"""The hook-event adapter's three obligations: deny survives, env is forwarded, absence is loud.

Each test here corresponds to a numbered obligation in `hook_http`'s module docstring. They
are contract tests, not coverage: every one of them asserts a property whose violation is
silent in production.
"""

from __future__ import annotations

import json

from coordinator_core.warm import hook_http


# -- Obligation 1: a deny survives the round trip with its reason intact -----------------

def test_deny_carries_its_reason_verbatim():
    """The reason reaches the model. A generic string turns an explained refusal into an
    unexplained one, which is what agents route around."""
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "rm -rf outside the repo root",
            },
        }
    ).encode()
    out = hook_http.interpret_result("PreToolUse", frame)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "rm -rf outside the repo root"
    assert hso["hookEventName"] == "PreToolUse"


def test_deny_without_a_supplied_reason_still_denies():
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"decision": "deny"}}
    ).encode()
    out = hook_http.interpret_result("PreToolUse", frame)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"]


def test_no_objection_does_not_emit_an_explicit_allow():
    """An explicit `allow` overrides the operator's own permission settings. A guard with
    no objection is making the weaker claim, and must say so by staying silent."""
    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
    out = hook_http.interpret_result("PreToolUse", frame)
    assert "permissionDecision" not in out["hookSpecificOutput"]


# -- Obligation 2: the caller's env travels on the event, never from this process --------

def test_payload_env_comes_from_the_event():
    event = {
        "hook_event_name": "PreToolUse",
        "env": {"COORDINATOR_ALLOW_RM": "1", "PATH": "/usr/bin", "HOME": "/root"},
    }
    payload = hook_http.payload_from_event(event)
    assert payload["env"] == {"COORDINATOR_ALLOW_RM": "1"}


def test_unrelated_env_is_not_put_on_the_wire():
    """Forwarding the whole environ would ship session secrets on every hook fire."""
    event = {"env": {"AWS_SECRET_ACCESS_KEY": "s3kr1t", "COORDINATOR_ALLOW_RM": "1"}}
    payload = hook_http.payload_from_event(event)
    assert "AWS_SECRET_ACCESS_KEY" not in payload["env"]


def test_absent_event_env_yields_an_empty_mapping_not_a_missing_key():
    """THE INVISIBLE-DISARM CASE. `_override` falls back to ambient `os.environ` when the
    payload carries no `env`; on a resident server that ambient environ is the SERVER's,
    shared by every session it serves. An empty mapping is a truthful "the caller set no
    overrides" and keeps the fallback from ever firing."""
    payload = hook_http.payload_from_event({"hook_event_name": "PreToolUse"})
    assert payload["env"] == {}
    assert "env" in payload


def test_malformed_event_env_is_not_trusted_as_a_mapping():
    payload = hook_http.payload_from_event({"env": "COORDINATOR_ALLOW_RM=1"})
    assert payload["env"] == {}


def test_server_environ_is_never_read_by_the_forwarder(monkeypatch):
    """The pin C14c could not provide: an override present ONLY in this process's
    environment must not reach the payload. C14c's 9 tests pin the reader; nothing pinned
    the writer, so this exact leak passed every existing suite."""
    monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")
    payload = hook_http.payload_from_event({"hook_event_name": "PreToolUse"})
    assert payload["env"] == {}


def test_build_request_carries_no_engine_token():
    """The transport places the token from its header so both paths present one that the
    same `_serve_line` code judges. A second scheme here is the thing to avoid."""
    frame = json.loads(hook_http.build_request({"env": {}}, "guard.evaluate"))
    assert "_engine_token" not in frame
    assert frame["method"] == "guard.evaluate"


# -- Obligation 3: a guard that could not run never reads as one that passed -------------

def test_error_envelope_is_not_read_as_a_verdict():
    """`try_warm_dispatch` counts any well-formed JSON-RPC response as a served hit,
    INCLUDING an error. An unregistered op returns METHOD_NOT_FOUND and a naive caller
    reads it as no-objection."""
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such method"}}
    ).encode()
    out = hook_http.interpret_result("PreToolUse", frame)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "did not run" in out["hookSpecificOutput"]["additionalContext"]
    assert "-32601" in out["systemMessage"]


def test_unparseable_response_is_reported_not_swallowed():
    out = hook_http.interpret_result("PreToolUse", b"<html>502 Bad Gateway</html>")
    assert "did not run" in out["hookSpecificOutput"]["additionalContext"]


def test_result_that_is_not_an_object_is_reported():
    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"}).encode()
    out = hook_http.interpret_result("PreToolUse", frame)
    assert "did not run" in out["hookSpecificOutput"]["additionalContext"]


def test_unreachable_is_not_a_deny():
    """Denying on infrastructure failure would take Write/Edit/Bash away from every
    session the moment the server hiccups -- the unrepairable class."""
    out = hook_http.unreachable_response("PreToolUse", "connection refused")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_unreachable_is_not_silent():
    out = hook_http.unreachable_response("PreToolUse", "connection refused")
    assert out["suppressOutput"] is False
    assert "connection refused" in out["systemMessage"]
    assert "did not run" in out["hookSpecificOutput"]["additionalContext"]


def test_blocking_events_are_distinguished_from_advisory_ones():
    assert hook_http.is_blocking_event("PreToolUse")
    assert not hook_http.is_blocking_event("PostToolUse")
    assert not hook_http.is_blocking_event(None)
