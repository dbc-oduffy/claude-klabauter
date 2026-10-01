"""The write engine resolves each hard-deny at the policy point: warn by default,
deny only by flag, floor guards always deny, and a demoted deny never stops the scan."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.write_guards import engine

_LEVEL = "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL"
_PAYLOAD = {
    "tool_name": "Write",
    "tool_input": {"file_path": "CLAUDE.md"},
    "session_id": "",
    "cwd": "",
}


def _deny(text):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": text,
        }
    }


def _guard(name, priority, text):
    return engine._Guard(name, "hard-deny", ["Write"], priority, lambda p: _deny(text))


@pytest.fixture
def consumer(monkeypatch, tmp_path):
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "reg"))
    monkeypatch.delenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", raising=False)
    monkeypatch.delenv(_LEVEL, raising=False)
    machine_profile.reset_cache()

    def install(*guards):
        monkeypatch.setattr(engine, "_discover_guards", lambda: (list(guards), []))
        machine_profile.reset_cache()

    return install


def _decision(out):
    return out["hookSpecificOutput"]["permissionDecision"]


def test_no_flags_demotes_non_floor_deny_to_advisory(consumer):
    consumer(_guard("guard_doctrine_surface_edits", 10, "Doctrine edit."))
    out = engine.evaluate(dict(_PAYLOAD))
    assert _decision(out) == "allow"
    assert "Advisory" in out["hookSpecificOutput"]["additionalContext"]


def test_global_strict_denies(consumer, monkeypatch):
    consumer(_guard("guard_doctrine_surface_edits", 10, "Doctrine edit."))
    monkeypatch.setenv(_LEVEL, "strict")
    machine_profile.reset_cache()
    assert _decision(engine.evaluate(dict(_PAYLOAD))) == "deny"


def test_per_guard_strict_hardens_only_that_guard(consumer, monkeypatch):
    a = _guard("guard-a", 10, "A tripped.")
    b = _guard("guard-b", 20, "B tripped.")
    monkeypatch.setenv(_LEVEL, "warn")
    monkeypatch.setenv(_LEVEL + "_GUARD-A", "strict")
    consumer(a, b)
    assert _decision(engine.evaluate(dict(_PAYLOAD))) == "deny"
    consumer(b)
    assert _decision(engine.evaluate(dict(_PAYLOAD))) == "allow"


def test_floor_guard_denies_with_no_flags(consumer):
    consumer(_guard("block_consumed_handoff_edit", 10, "Consumed handoff."))
    assert _decision(engine.evaluate(dict(_PAYLOAD))) == "deny"


def test_demoted_deny_does_not_hide_a_later_floor_deny(consumer):
    consumer(
        _guard("guard_doctrine_surface_edits", 10, "Doctrine edit."),
        _guard("block_consumed_handoff_edit", 20, "Consumed handoff."),
    )
    out = engine.evaluate(dict(_PAYLOAD))
    assert _decision(out) == "deny"
    assert "Consumed handoff" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_held_advisory_returned_or_prepended_in_aggregate(consumer):
    consumer(_guard("guard_doctrine_surface_edits", 10, "Doctrine edit."))
    assert _decision(engine.evaluate(dict(_PAYLOAD))) == "allow"
    fired = engine.evaluate(dict(_PAYLOAD), aggregate=True)
    assert len(fired) == 1 and _decision(fired[0]) == "allow"
