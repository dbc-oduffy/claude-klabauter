"""Every Bash-seam deny carries the guard key for an EM caller and none for a subagent."""
from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._advisory_value import AdvisoryValue
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    EscalationRoute,
    Override,
)

_SUBAGENT = {"agent_id": "deadbeef0123", "agent_type": "coordinator:executor"}


def _deny(name: str) -> AlternativeRecord:
    return AlternativeRecord(
        verdict_kind="deny",
        summary="Refused for a test.",
        members=(EscalationRoute("the refusal", "a different route"), Override("COORD_TEST_OVERRIDE")),
        guard_name=name,
    )


def _seed_readme(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    d = tmp_path / ".coordinator-claude-settings" / "coordinator-claude" / "docs" / "wiki" / "guard-routes"
    d.mkdir(parents=True)
    (d / "README.md").write_text("index", encoding="utf-8")


def _fire(monkeypatch, name: str, fail_closed: bool, identity: dict) -> str:
    entry = dispatch.GuardEntry(
        name,
        lambda: _deny(name),
        fail_closed,
        dispatch.GuardBand.CONFINEMENT_DENY if fail_closed else dispatch.GuardBand.ADVISORY_REWRITE,
        AdvisoryValue.HOST_INDEPENDENT,
        matchers=COMMAND_TOOL_NAMES,
    )
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "session_id": "route-key-%s" % name,
        "cwd": None,
        **identity,
    }
    out = dispatch.evaluate_payload_json(json.dumps(payload), host_is_windows=False)
    assert isinstance(out, dict)
    return out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "name,fail_closed", [("synthetic-fail-closed", True), ("synthetic-non-sentinel", False)]
)
def test_em_deny_carries_exactly_one_route_key(tmp_path, monkeypatch, name, fail_closed):
    _seed_readme(tmp_path, monkeypatch)
    reason = _fire(monkeypatch, name, fail_closed, {})
    assert reason.count("gr:%s" % name) == 1
    assert reason.count("gr:") == 1


@pytest.mark.parametrize(
    "name,fail_closed", [("synthetic-fail-closed", True), ("synthetic-non-sentinel", False)]
)
def test_subagent_deny_carries_identity_but_no_route(tmp_path, monkeypatch, name, fail_closed):
    _seed_readme(tmp_path, monkeypatch)
    reason = _fire(monkeypatch, name, fail_closed, _SUBAGENT)
    assert "gr:" not in reason
    assert "guard-routes" not in reason
    assert "Guard: `%s`." % name in reason


def test_em_deny_without_readme_carries_no_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    reason = _fire(monkeypatch, "synthetic-non-sentinel", False, {})
    assert "gr:" not in reason


def test_em_deny_drops_override_note_only_when_route_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    without = _fire(monkeypatch, "synthetic-non-sentinel", False, {})
    _seed_readme(tmp_path, monkeypatch)
    with_route = _fire(monkeypatch, "synthetic-non-sentinel", False, {})
    assert "guard-override-keys" in without
    assert "guard-override-keys" not in with_route
