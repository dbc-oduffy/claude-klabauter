"""The dispatcher is the only renderer of AlternativeRecord results.

Drives `evaluate_payload_json` over a one-entry synthetic chain, so the record
goes through the production seam: render, then suppression, dedupe and the
unlock annotation on the rendered envelope.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _audience, dispatch
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry
from coordinator_core.guard_routes import keys
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    EscalationRoute,
    Override,
    ReadyCommand,
)

_ENV_VAR = "COORDINATOR_FAKE_RECORD_OVERRIDE"
_OVERRIDE_POINTER = "override keys"


def _record(*, with_override: bool = True, with_route: bool = True) -> AlternativeRecord:
    members = [ReadyCommand(text="git status")]
    if with_route:
        members.append(EscalationRoute(report_what="the blocked command", authorize_what="the command"))
    if with_override:
        members.append(Override(env_var=_ENV_VAR))
    return AlternativeRecord(
        verdict_kind="deny", summary="synthetic deny.", members=tuple(members), guard_name="fake"
    )


def _fire(monkeypatch, tmp_path, result, *, subagent: bool = False):
    chain = [GuardEntry("fake-record-guard", lambda: result, True, GuardBand.CONFINEMENT_DENY)]
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: list(chain))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "session_id": "dispatch-renders-records",
        "cwd": str(tmp_path),
    }
    if subagent:
        payload["agent_id"] = "deadbeefdeadbeef"
        payload["agent_type"] = "coordinator:executor"
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _reason(envelope) -> str:
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


def test_em_payload_renders_the_override(monkeypatch, tmp_path):
    monkeypatch.setattr(keys, "routes_index_reachable", lambda: False)
    reason = _reason(_fire(monkeypatch, tmp_path, _record()))
    assert "synthetic deny." in reason
    assert _OVERRIDE_POINTER in reason


def test_em_payload_with_reachable_routes_index_carries_the_route_not_the_override(monkeypatch, tmp_path):
    monkeypatch.setattr(keys, "routes_index_reachable", lambda: True)
    reason = _reason(_fire(monkeypatch, tmp_path, _record()))
    assert "synthetic deny." in reason
    assert "Route key gr:fake-record-guard" in reason
    assert _OVERRIDE_POINTER not in reason


@pytest.mark.parametrize("unresolved", [False, True])
def test_subagent_payload_gets_a_route_and_no_override(monkeypatch, tmp_path, unresolved):
    if unresolved:
        def boom(*a, **k):
            raise RuntimeError("identity unavailable")

        monkeypatch.setattr(
            "coordinator_core.write_guards._subagent_identity._resolve_subagent_identity", boom
        )
    reason = _reason(_fire(monkeypatch, tmp_path, _record(), subagent=True))
    assert _OVERRIDE_POINTER not in reason
    assert "the blocked command" in reason


def test_record_without_override_resolves_no_audience(monkeypatch, tmp_path):
    def forbidden(payload):
        raise AssertionError("audience resolved for a record with no Override")

    monkeypatch.setattr(_audience, "resolve_audience", forbidden)
    reason = _reason(_fire(monkeypatch, tmp_path, _record(with_override=False)))
    assert "synthetic deny." in reason
    assert _OVERRIDE_POINTER not in reason


def _legacy(decision):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": "legacy dict text",
        }
    }


def test_unpinned_deny_dict_crash_denies(monkeypatch, tmp_path):
    reason = _reason(_fire(monkeypatch, tmp_path, _legacy("deny")))
    assert "legacy dict text" not in reason
    assert "fake-record-guard" in reason


def test_unpinned_non_deny_dict_crash_denies(monkeypatch, tmp_path):
    reason = _reason(_fire(monkeypatch, tmp_path, _legacy("allow")))
    assert "legacy dict text" not in reason
    assert "fake-record-guard" in reason


def test_pinned_non_deny_dict_passes_through(monkeypatch, tmp_path):
    monkeypatch.setitem(dispatch._PINNED_DICT_RETURNS, "fake-record-guard", "test pin")
    envelope = _fire(monkeypatch, tmp_path, _legacy("allow"))
    assert _reason(envelope).startswith("legacy dict text")


def test_pinned_deny_dict_still_crash_denies(monkeypatch, tmp_path):
    monkeypatch.setitem(dispatch._PINNED_DICT_RETURNS, "fake-record-guard", "test pin")
    assert "legacy dict text" not in _reason(_fire(monkeypatch, tmp_path, _legacy("deny")))


def test_crash_deny_is_a_rendered_record_with_a_route():
    envelope = dispatch._crash_deny("some-guard", ValueError("boom"))
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "some-guard" in hso["permissionDecisionReason"]
    assert "whoever dispatched you" in hso["permissionDecisionReason"]


def test_dispatch_has_no_module_scope_guard_messages_import():
    tree = ast.parse(Path(dispatch.__file__).read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(("coordinator_core.guard_messages", "coordinator_core.bash_guards._audience"))
    ]
    assert not offenders, "module-scope import of the render path at lines %s" % offenders
