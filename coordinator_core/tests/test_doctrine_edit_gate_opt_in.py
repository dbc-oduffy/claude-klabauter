"""The doctrine-edit approval gate is default-on: off only when
``coordinator.feature.doctrine_edit_gate`` is explicitly ``off``."""

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
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
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


@pytest.mark.parametrize("profile", ["author", "consumer"])
def test_defaults_on_with_no_config(author_box, monkeypatch, profile):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", profile)
    mp.reset_cache()
    assert mp.feature_enabled("doctrine_edit_gate") is True
    assert _is_deny(block_approval_sentinel_creation.check(_touch_payload()))


def test_forged_sentinel_write_is_refused_with_no_config(author_box, monkeypatch):
    _set_gate(monkeypatch, None)
    assert _is_deny(guard_doctrine_surface_edits.check(_sentinel_write_payload(author_box)))
    assert _is_deny(block_approval_sentinel_creation.check(_touch_payload()))


def test_genuine_pm_sentinel_still_passes(author_box, monkeypatch, tmp_path):
    _set_gate(monkeypatch, None)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(guard_doctrine_surface_edits, "_git_root", lambda: str(repo))
    monkeypatch.setattr(guard_doctrine_surface_edits, "read_content_root", lambda: "")
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "CLAUDE.md")}}
    assert _is_deny(guard_doctrine_surface_edits.check(payload))
    (repo / SENTINEL).write_text("", encoding="utf-8")  # the PM's out-of-band act
    assert guard_doctrine_surface_edits.check(payload) is None


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


def test_every_enforcement_point_allows_when_off(author_box, monkeypatch):
    _set_gate(monkeypatch, "off")
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
