"""Audience and level behaviour of the converted write-bump and doctrine-surface guards."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import (
    bump_foreign_repo_write as foreign,
    bump_outside_repo_write as outside,
    guard_doctrine_surface_bash_write as doctrine,
)
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.render import render

_MESSAGE = "Coordinator guard — instead: writing into `x` is refused.\n\nGuard: `g`."


def _set_level(monkeypatch, level: str) -> None:
    monkeypatch.setattr(machine_profile, "guard_level", lambda name: level)
    monkeypatch.setattr(machine_profile, "out_of_repo_write_blocked", lambda: False)


def _foreign(monkeypatch, level: str):
    _set_level(monkeypatch, level)
    return foreign._levelled_deny("bump-foreign-repo-write", _MESSAGE)


def _outside(monkeypatch, level: str):
    _set_level(monkeypatch, level)
    return outside._levelled_deny(
        "bump-outside-repo-write",
        _MESSAGE,
        outside.apply_cross_repo_level,
    )


@pytest.mark.parametrize("build", [_foreign, _outside])
def test_strict_returns_a_record_whose_em_render_is_the_message_verbatim(monkeypatch, build):
    record = build(monkeypatch, "strict")
    assert isinstance(record, AlternativeRecord)
    env = render(record, Audience.EM)["hookSpecificOutput"]
    assert env["permissionDecision"] == "deny"
    assert env["permissionDecisionReason"] == _MESSAGE


@pytest.mark.parametrize("build", [_foreign, _outside])
def test_subagent_render_offers_a_route_and_no_override(monkeypatch, build):
    record = build(monkeypatch, "strict")
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    assert not any(isinstance(m, Override) for m in record.members)
    env = render(record, Audience.SUBAGENT)["hookSpecificOutput"]
    assert env["permissionDecision"] == "deny"


@pytest.mark.parametrize("build", [_foreign, _outside])
def test_off_level_allows_silently(monkeypatch, build):
    assert build(monkeypatch, "off") is None


@pytest.mark.parametrize("build", [_foreign, _outside])
def test_warn_level_returns_the_advisory_envelope_not_a_record(monkeypatch, build):
    result = build(monkeypatch, "warn")
    assert isinstance(result, dict)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "additionalContext" in hso


def test_doctrine_deny_is_a_record_at_strict_and_an_envelope_otherwise(monkeypatch):
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > CLAUDE.md"}, "cwd": ""}
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)

    _set_level(monkeypatch, "strict")
    record = doctrine.check(payload, ["CLAUDE.md"])
    assert isinstance(record, AlternativeRecord)
    reason = render(record, Audience.EM)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("BLOCKED: writes a governed doctrine surface.")

    _set_level(monkeypatch, "warn")
    warned = doctrine.check(payload, ["CLAUDE.md"])
    assert isinstance(warned, dict)
    assert warned["hookSpecificOutput"]["permissionDecision"] == "allow"

    _set_level(monkeypatch, "off")
    assert doctrine.check(payload, ["CLAUDE.md"]) is None


def test_doctrine_still_allows_an_unrelated_command(monkeypatch):
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    _set_level(monkeypatch, "strict")
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > notes.txt"}, "cwd": ""}
    assert doctrine.check(payload, ["CLAUDE.md"]) is None


def test_doctrine_sizing_deny_is_a_record_with_the_legacy_text():
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > state/sizings/a.yaml"}, "cwd": ""}
    record = doctrine.check(payload, [])
    assert isinstance(record, AlternativeRecord)
    env = render(record, Audience.EM)["hookSpecificOutput"]
    assert env["permissionDecisionReason"] == doctrine._SIZING_RECORD_DENY


def test_doctrine_advisory_is_an_advisory_record(monkeypatch):
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: False)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)
    _set_level(monkeypatch, "warn")
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > CLAUDE.md"}, "cwd": ""}
    record = doctrine.check(payload, ["CLAUDE.md"])
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "advisory"
    env = render(record, Audience.EM)["hookSpecificOutput"]
    assert env["additionalContext"] == doctrine.doctrine_surface_advisory()["hookSpecificOutput"]["additionalContext"]
