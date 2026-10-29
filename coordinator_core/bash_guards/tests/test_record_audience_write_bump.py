"""Level behaviour of the converted write-bump and doctrine-surface guards.

All three guards are advisory-by-ruling (deny_registry.ADVISORY_BY_RULING; 307ea22403 DR-475,
classified in b17ec35251): at every guard level the would-be deny comes back as one allow-plus-context
line, never an AlternativeRecord or a deny envelope.
"""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import (
    bump_foreign_repo_write as foreign,
    bump_outside_repo_write as outside,
    guard_doctrine_surface_bash_write as doctrine,
)
from coordinator_core.bash_guards._write_bump_message import CROSS_REPO_WARN_RISK
from coordinator_core.guard_messages.alternative_record import AlternativeRecord

_MESSAGE = "Coordinator guard — instead: writing into `x` is refused.\n\nGuard: `g`."
_ADVISORY = "Advisory: " + CROSS_REPO_WARN_RISK


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
def test_strict_returns_the_advisory_envelope_not_a_record(monkeypatch, build):
    result = build(monkeypatch, "strict")
    assert not isinstance(result, AlternativeRecord)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"] == _ADVISORY


@pytest.mark.parametrize("build", [_foreign, _outside])
def test_advisory_offers_no_override_key(monkeypatch, build):
    hso = build(monkeypatch, "strict")["hookSpecificOutput"]
    assert set(hso) == {"hookEventName", "additionalContext", "permissionDecision"}


@pytest.mark.parametrize("build", [_foreign, _outside])
def test_off_level_still_returns_the_advisory(monkeypatch, build):
    result = build(monkeypatch, "off")
    assert result["hookSpecificOutput"]["additionalContext"] == _ADVISORY


@pytest.mark.parametrize("build", [_foreign, _outside])
def test_warn_level_returns_the_advisory_envelope_not_a_record(monkeypatch, build):
    result = build(monkeypatch, "warn")
    assert isinstance(result, dict)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "additionalContext" in hso


def test_doctrine_deny_is_an_advisory_envelope_at_every_level(monkeypatch):
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > CLAUDE.md"}, "cwd": ""}
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)

    for level in ("strict", "warn", "off"):
        _set_level(monkeypatch, level)
        result = doctrine.check(payload, ["CLAUDE.md"])
        assert isinstance(result, dict), level
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow", level
        assert hso["additionalContext"].startswith("Advisory: this command writes a governed"), level


def test_doctrine_still_allows_an_unrelated_command(monkeypatch):
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    _set_level(monkeypatch, "strict")
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > notes.txt"}, "cwd": ""}
    assert doctrine.check(payload, ["CLAUDE.md"]) is None


def test_doctrine_sizing_deny_is_an_advisory_envelope():
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > state/sizings/a.yaml"}, "cwd": ""}
    result = doctrine.check(payload, [])
    assert isinstance(result, dict)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"].startswith("Advisory: this command writes the sizing")


def test_doctrine_advisory_is_an_advisory_record(monkeypatch):
    from coordinator_core.guard_messages.render import render
    from coordinator_core.guard_messages.alternative_record import Audience

    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: False)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)
    _set_level(monkeypatch, "warn")
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > CLAUDE.md"}, "cwd": ""}
    record = doctrine.check(payload, ["CLAUDE.md"])
    assert isinstance(record, AlternativeRecord)
    assert record.verdict_kind == "advisory"
    env = render(record, Audience.EM)["hookSpecificOutput"]
    assert env["additionalContext"] == doctrine.doctrine_surface_advisory()["hookSpecificOutput"]["additionalContext"]
