"""Tests for `coordinator_core.ops.warm_guard_evaluate` — the registered
`warm_guard.evaluate` op (state/handoffs/2026-08-23-the-warm-guard-op-gets-registered.md).

THE LOAD-BEARING TEST IN THIS FILE is `test_boundary...` below. Everything else pins the
response-shape contract (deny survives, no-objection carries no key, a genuine hit is
observable through `try_warm_guard_dispatch`).

This suite exercises the REAL registered op and the REAL guard chain
(`bash_guards.dispatch.evaluate_payload_json`) — never a monkeypatched stand-in for
`evaluate_payload_json` itself, because the whole point of "boundary-deletion" coverage is
to prove the ACTUAL chain forwards the payload's `env` into the checks that call
`dispatch_checks._override()`. A stand-in chain would prove nothing about that wiring.

Uses `check_no_verify`'s own override key (`COORDINATOR_OVERRIDE_NO_VERIFY`) as the guard
under test: it needs no git repository, no filesystem target, and no network — a bare
`git commit --no-verify -m x` string is enough to trip it, which keeps this suite fast and
independent of the working tree it happens to run in.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from coordinator_core.ops import warm_guard_evaluate
from coordinator_core.warm import hook_http
from coordinator_core.warm.entry_seam import try_warm_guard_dispatch

#: Deliberately SCOPED (`-- foo.py`) so `check_git_commit_safe_commit_advise` -- an
#: UNCONDITIONAL advisory-deny on any bare, unscoped `git commit`, independent of both
#: this test's override and any repo state -- never fires alongside it. An unscoped form
#: would make the assertions below fail for a reason that has nothing to do with
#: `COORDINATOR_OVERRIDE_NO_VERIFY` or the env-forwarding boundary this suite pins.
_NO_VERIFY_CMD = "git commit --no-verify -m x -- foo.py"


def _event(
    cmd: str,
    *,
    env: dict | None = None,
    session_id: str = "s-warm-guard",
    cwd: str = "C:/Windows/Temp",
) -> dict:
    # `cwd` deliberately defaults OUTSIDE this repo's own working tree: several
    # guards in the real chain (e.g. the shared-staged-index commit-safety advise)
    # inspect the actual git status of whatever `cwd` resolves to, and this repo's
    # own tree carries real staged/uncommitted work while these tests run. A
    # non-repo `cwd` keeps every guard OTHER than the one under test a clean no-op,
    # so `check_no_verify`'s own verdict is the only one that can fire.
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "cwd": cwd,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "env": env or {},
    }


def _call(event: dict) -> dict:
    """Drive the REAL registered handler, synchronously, the way `ipc`'s event loop
    would await it -- never the raw `evaluate_payload_json` function directly."""
    payload = hook_http.payload_from_event(event)
    return asyncio.run(
        warm_guard_evaluate._warm_guard_evaluate({"payload": payload})
    )


class TestBoundaryDeletion:
    """AC: 'a server whose OWN environ carries an override the forwarded event does NOT
    carry -- the guard must NOT see that override.' The doe-claude-em-named pin."""

    def test_server_environ_override_is_not_seen(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_NO_VERIFY", "1")

        result = _call(_event(_NO_VERIFY_CMD, env={}))

        assert result.get("permissionDecision") == "deny"
        assert "no-verify" in result["permissionDecisionReason"].lower() or (
            "bypass" in result["permissionDecisionReason"].lower()
        )

    def test_caller_override_is_honoured_when_the_event_carries_it(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        result = _call(_event(_NO_VERIFY_CMD, env={"COORDINATOR_OVERRIDE_NO_VERIFY": "1"}))

        assert result == {}

    def test_caller_override_wins_over_a_conflicting_server_environ(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_NO_VERIFY", "0")

        result = _call(_event(_NO_VERIFY_CMD, env={"COORDINATOR_OVERRIDE_NO_VERIFY": "1"}))

        assert result == {}

    def test_caller_absence_wins_over_a_permissive_server_environ(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_NO_VERIFY", "1")

        result = _call(_event(_NO_VERIFY_CMD, env={"COORDINATOR_ALLOW_RM": "1"}))

        assert result.get("permissionDecision") == "deny"


class TestVerdictShapes:
    def test_a_deny_survives_with_its_reason(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        result = _call(_event(_NO_VERIFY_CMD))

        assert result["permissionDecision"] == "deny"
        assert result["permissionDecisionReason"]

    def test_a_no_objection_carries_no_decision_key(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        result = _call(_event("echo probe"))

        assert result == {}
        assert "permissionDecision" not in result


class TestMalformedPayloadRaises:
    """Never fabricate a no-objection on an internal failure -- an absent/malformed
    payload must surface loudly (a JSON-RPC error via `ipc`, once dispatched through the
    registry), not silently allow."""

    def test_missing_payload_raises(self):
        with pytest.raises(TypeError):
            asyncio.run(warm_guard_evaluate._warm_guard_evaluate({}))

    def test_non_dict_payload_raises(self):
        with pytest.raises(TypeError):
            asyncio.run(warm_guard_evaluate._warm_guard_evaluate({"payload": "nope"}))


class TestVerdictFromEnvelopeUnit:
    """Direct unit coverage of the narrowing function, independent of the guard chain."""

    def test_none_is_no_objection(self):
        assert warm_guard_evaluate._verdict_from_envelope(None) == {}

    def test_allow_envelope_is_no_objection_never_forwarded_as_allow(self):
        envelope = {
            "hookSpecificOutput": {
                "permissionDecision": "allow",
                "permissionDecisionReason": "advisory note",
                "additionalContext": "some advisory text",
            }
        }
        result = warm_guard_evaluate._verdict_from_envelope(envelope)
        assert result == {}
        assert "permissionDecision" not in result

    def test_deny_envelope_is_flattened(self):
        envelope = {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "blocked",
            }
        }
        assert warm_guard_evaluate._verdict_from_envelope(envelope) == {
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked",
        }

    def test_deny_with_no_reason_gets_a_fallback_reason(self):
        envelope = {"hookSpecificOutput": {"permissionDecision": "deny"}}
        result = warm_guard_evaluate._verdict_from_envelope(envelope)
        assert result["permissionDecision"] == "deny"
        assert result["permissionDecisionReason"]

    def test_list_envelope_raises(self):
        with pytest.raises(TypeError):
            warm_guard_evaluate._verdict_from_envelope([{"hookSpecificOutput": {}}])

    def test_non_dict_non_list_raises(self):
        with pytest.raises(TypeError):
            warm_guard_evaluate._verdict_from_envelope("not-a-dict")


class TestRealWarmHit:
    """AC: `try_warm_guard_dispatch("warm_guard.evaluate", ...)` returns `hit=True`,
    verified by a test, not by inspection -- driven through the REAL registry via
    `coordinator_core.ipc.dispatch_message`, not a monkeypatched stand-in envelope."""

    def _server_side_dispatch(self, msg: dict) -> dict:
        from coordinator_core import ipc

        return asyncio.run(ipc.dispatch_message(msg))

    def test_a_real_hit_with_a_deny_verdict(self, monkeypatch):
        from coordinator_core.warm import client

        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)
        monkeypatch.setattr(client, "try_warm_dispatch", lambda msg: self._server_side_dispatch(msg))
        monkeypatch.setattr(
            "coordinator_core.warm.entry_seam._trigger_listener_boot", lambda: None
        )

        payload = hook_http.payload_from_event(_event(_NO_VERIFY_CMD))
        outcome = try_warm_guard_dispatch("warm_guard.evaluate", {"payload": payload})

        assert outcome.hit is True
        assert "error" not in outcome.response
        assert outcome.response["result"]["permissionDecision"] == "deny"

    def test_a_real_hit_with_a_no_objection_verdict(self, monkeypatch):
        from coordinator_core.warm import client

        monkeypatch.setattr(client, "try_warm_dispatch", lambda msg: self._server_side_dispatch(msg))
        monkeypatch.setattr(
            "coordinator_core.warm.entry_seam._trigger_listener_boot", lambda: None
        )

        payload = hook_http.payload_from_event(_event("echo probe"))
        outcome = try_warm_guard_dispatch("warm_guard.evaluate", {"payload": payload})

        assert outcome.hit is True
        assert "error" not in outcome.response
        assert "permissionDecision" not in outcome.response["result"]


class TestColdPathUnchanged:
    """AC: cold-path behaviour is unchanged for every caller that never passes a payload
    at all -- this op is additive; it does not touch `evaluate_payload_json`'s own
    default-argument behaviour or any caller that invokes it directly (raw string, no
    `env` key), which every pre-existing dispatch test already covers. This test is a
    narrow, local restatement: a bare cold-shaped JSON payload with no `env` key
    reproduces the exact same deny as before this op existed.
    """

    def test_bare_payload_with_no_env_key_still_denies(self, monkeypatch):
        from coordinator_core.bash_guards.dispatch import evaluate_payload_json

        monkeypatch.setenv("COORDINATOR_OVERRIDE_NO_VERIFY", "0")

        raw = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": _NO_VERIFY_CMD},
                "session_id": "cold-caller",
                "cwd": "C:/Windows/Temp",
            }
        )
        out = evaluate_payload_json(raw)

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
