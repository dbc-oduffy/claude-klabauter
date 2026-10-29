"""A Write/Edit that turns the doctrine-edit gate off, lowers a guard level or
turns a coordinator feature off lands with an advisory naming it; strengthening
and unrelated writes land with nothing."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.write_guards import guard_doctrine_surface_edits as write_guard

KEY = "coordinator.feature.doctrine_edit_gate"


@pytest.fixture()
def box(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(write_guard, "_git_root", lambda: str(repo))
    monkeypatch.setattr(
        write_guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "UNRESOLVED", "message": ""}
    )
    mp.reset_cache()
    yield reg, repo
    mp.reset_cache()


def _write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


def _advises(result, what):
    assert result is not None
    out = result["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert what in out["additionalContext"]
    assert "PM" not in out["additionalContext"]


@pytest.mark.parametrize("name", ["registry.local.toml", "registry.toml"])
def test_registry_file_write_off_lands_with_an_advisory(box, name):
    path = box[0] / name
    _advises(write_guard.check(_write(path, f'"{KEY}" = \'off\'\n')), "turns off the doctrine-edit gate")
    _advises(
        write_guard.check(_write(path, '[coordinator.feature]\ndoctrine_edit_gate = "off"\n')),
        "turns off the doctrine-edit gate",
    )


def test_registry_file_edit_off_lands_with_an_advisory(box):
    path = box[0] / "registry.local.toml"
    path.write_text(f'"{KEY}" = \'on\'\n', encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": "'on'", "new_string": "'off'"},
    }
    _advises(write_guard.check(payload), "turns off the doctrine-edit gate")


def test_registry_file_on_and_unrelated_writes_land_quietly(box):
    path = box[0] / "registry.local.toml"
    assert write_guard.check(_write(path, f'"{KEY}" = \'on\'\n')) is None
    assert write_guard.check(_write(path, '"repos.x" = \'/p\'\n')) is None


def test_already_off_file_unrelated_edit_lands_quietly(box):
    path = box[0] / "registry.local.toml"
    path.write_text(f'"{KEY}" = \'off\'\n"a" = \'1\'\n', encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": "'1'", "new_string": "'2'"},
    }
    assert write_guard.check(payload) is None


def test_registry_file_level_lowering_lands_with_an_advisory(box):
    path = box[0] / "registry.local.toml"
    path.write_text('"coordinator.guard_level" = "strict"\n', encoding="utf-8")
    _advises(write_guard.check(_write(path, '"coordinator.guard_level" = "off"\n')), "lowers the guard level")
    edit = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": "strict", "new_string": "warn"},
    }
    _advises(write_guard.check(edit), "lowers the guard level")
    assert write_guard.check(_write(path, '"coordinator.guard_level" = "strict"\n')) is None


def _settings(box, where):
    d = box[1] / where if where == ".claude" else box[1].parent / "home" / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.parametrize("where", [".claude", "home"])
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_settings_env_weakening_lands_with_an_advisory(box, where, name, tool):
    path = _settings(box, where) / name
    path.write_text('{"env": {"MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING": "on", "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL": "strict"}}', encoding="utf-8")
    new_text = path.read_text().replace('FEATURE_PUBLISHING": "on"', 'FEATURE_PUBLISHING": "off"')
    lvl_text = path.read_text().replace('LEVEL": "strict"', 'LEVEL": "warn"')
    ok_text = '{"env": {"MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING": "on", "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL": "strict"}, "theme": "x"}'

    def payload(text):
        if tool == "Write":
            return _write(path, text)
        edit = {"old_string": path.read_text(), "new_string": text}
        if tool == "Edit":
            return {"tool_name": "Edit", "tool_input": {"file_path": str(path), **edit}}
        return {"tool_name": "MultiEdit", "tool_input": {"file_path": str(path), "edits": [edit]}}

    _advises(write_guard.check(payload(new_text)), "turns off a coordinator feature")
    _advises(write_guard.check(payload(lvl_text)), "lowers the guard level")
    assert write_guard.check(payload(ok_text)) is None
