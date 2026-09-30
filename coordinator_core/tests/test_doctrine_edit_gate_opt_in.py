"""The doctrine-edit approval gate is opt-in: off unless
``coordinator.feature.doctrine_edit_gate`` is ``on``, on author boxes too."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.bash_guards import (
    block_approval_sentinel_creation,
    guard_doctrine_surface_bash_write,
)
from coordinator_core.hooks import guard_doctrine_surface_bash_write as hook_bash_write
from coordinator_core.write_guards import guard_doctrine_surface_edits

GATE_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE"
SENTINEL = ".coordinator-doctrine-edit-approved"


@pytest.fixture()
def author_box(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    for key in list(__import__("os").environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", "author")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
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


def _sentinel_write_payload(home):
    return {"tool_name": "Write", "tool_input": {"file_path": str(home / SENTINEL)}}


def _touch_payload():
    return {"tool_name": "Bash", "tool_input": {"command": "touch " + SENTINEL}}


def _bash_write_payload():
    return {"tool_name": "Bash", "tool_input": {"command": "cat x > CLAUDE.md"}}


def _is_deny(result):
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def test_defaults_off_on_an_author_profile(author_box):
    assert mp.machine_profile() == "author"
    assert mp.feature_enabled("doctrine_edit_gate") is False
    assert mp.feature_enabled("publishing") is True
    assert mp.feature_enabled("cross_repo_memos") is True


def test_explicit_on_enables_and_off_disables(author_box, monkeypatch):
    _set_gate(monkeypatch, "on")
    assert mp.feature_enabled("doctrine_edit_gate") is True
    _set_gate(monkeypatch, "off")
    assert mp.feature_enabled("doctrine_edit_gate") is False


@pytest.mark.parametrize("value", [None, "off"])
def test_every_enforcement_point_allows_when_off(author_box, monkeypatch, value):
    _set_gate(monkeypatch, value)
    home = author_box
    assert guard_doctrine_surface_edits.check(_edit_payload(home)) is None
    assert guard_doctrine_surface_edits.check(_sentinel_write_payload(home)) is None
    assert block_approval_sentinel_creation.check(_touch_payload()) is None
    assert not _is_deny(
        guard_doctrine_surface_bash_write.check(_bash_write_payload(), ["CLAUDE.md"])
    )
    assert hook_bash_write.evaluate(_bash_write_payload()) is None


def test_every_enforcement_point_denies_when_on(author_box, monkeypatch):
    _set_gate(monkeypatch, "on")
    home = author_box
    assert _is_deny(guard_doctrine_surface_edits.check(_edit_payload(home)))
    assert _is_deny(guard_doctrine_surface_edits.check(_sentinel_write_payload(home)))
    assert _is_deny(block_approval_sentinel_creation.check(_touch_payload()))
    assert _is_deny(
        guard_doctrine_surface_bash_write.check(_bash_write_payload(), ["CLAUDE.md"])
    )
    assert hook_bash_write.evaluate(_bash_write_payload()) is not None


def test_liveness_probe_trigger_fires_while_gate_is_off(author_box, monkeypatch):
    _set_gate(monkeypatch, None)
    assert _is_deny(block_approval_sentinel_creation.check_ungated(_touch_payload()))
