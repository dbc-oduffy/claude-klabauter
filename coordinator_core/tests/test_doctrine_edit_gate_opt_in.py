"""The doctrine-edit gate is default-on: off only when
``coordinator.feature.doctrine_edit_gate`` is explicitly ``off``."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.bash_guards import guard_doctrine_surface_bash_write
from coordinator_core.write_guards import guard_doctrine_surface_edits

GATE_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE"


@pytest.fixture()
def author_box(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    for key in list(os.environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", "author")
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(
        guard_doctrine_surface_edits,
        "compute_repo_identity_gate",
        lambda *a, **k: {"verdict": "UNRESOLVED", "message": ""},
    )
    mp.reset_cache()
    yield home
    mp.reset_cache()


def _set_gate(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(GATE_ENV, value)
    mp.reset_cache()


def _edit_payload(home):
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(home / ".claude" / "CLAUDE.md")},
    }


def _bash_write_payload():
    return {"tool_name": "Bash", "tool_input": {"command": "cat x > CLAUDE.md"}}


def _is_deny(result):
    result = envelope_of(result, "em")
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def _is_advisory(result):
    return (
        result is not None
        and "permissionDecision" not in result["hookSpecificOutput"]
        and "advisory" in result["hookSpecificOutput"]["additionalContext"]
    )


@pytest.mark.parametrize("profile", ["author", "consumer"])
def test_defaults_on_with_no_config(author_box, monkeypatch, profile):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", profile)
    mp.reset_cache()
    assert mp.feature_enabled("doctrine_edit_gate") is True
    assert _is_advisory(guard_doctrine_surface_edits.check(_edit_payload(author_box)))


def test_defaults_on_an_author_profile(author_box):
    assert mp.machine_profile() == "author"
    assert mp.feature_enabled("doctrine_edit_gate") is True
    assert mp.feature_enabled("publishing") is True
    assert mp.feature_enabled("cross_repo_memos") is True


def test_explicit_on_enables_and_off_disables(author_box, monkeypatch):
    _set_gate(monkeypatch, "on")
    assert mp.feature_enabled("doctrine_edit_gate") is True
    _set_gate(monkeypatch, "off")
    assert mp.feature_enabled("doctrine_edit_gate") is False


def test_every_enforcement_point_is_quiet_when_off(author_box, monkeypatch):
    _set_gate(monkeypatch, "off")
    assert guard_doctrine_surface_edits.check(_edit_payload(author_box)) is None
    assert not _is_deny(
        guard_doctrine_surface_bash_write.check(_bash_write_payload(), ["CLAUDE.md"])
    )


def test_enforcement_points_when_on(author_box, monkeypatch):
    _set_gate(monkeypatch, "on")
    assert _is_advisory(guard_doctrine_surface_edits.check(_edit_payload(author_box)))
    assert not _is_deny(guard_doctrine_surface_edits.check(_edit_payload(author_box)))
    assert _is_deny(
        guard_doctrine_surface_bash_write.check(_bash_write_payload(), ["CLAUDE.md"])
    )
