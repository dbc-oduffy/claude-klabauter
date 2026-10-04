"""Tests for coordinator_core.content_root: resolution and legacy-config migration."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import content_root as cr
from coordinator_core.machine_resolver import registry_get

LEGACY_KEY = "repos.content_root"  # private-name-ok: compat-fallback
LEGACY_ENGINE_KEY = "engine.working_repos.content_root"  # private-name-ok: compat-fallback
LEGACY_POINTER = ".coordinator-content-root"  # private-name-ok: compat-fallback


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    settings = tmp_path / "settings"
    (settings / "machine-local").mkdir(parents=True)
    home.mkdir()
    for var in ("MACHINE_LOCAL_REGISTRY_DIR", "CLAUDE_PLUGIN_ROOT", "USERPROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REPOS_CONTENT_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    return settings / "machine-local"


def _registry(box, text: str) -> None:
    (box / "registry.local.toml").write_text(text, encoding="utf-8")


def test_fresh_box_writes_nothing(box):
    result = cr.migrate_legacy_config()
    assert not result.migrated
    assert cr.read_content_root() == ""
    assert sorted(p.name for p in box.iterdir()) == []


def test_legacy_pointer_only_is_migrated(box, tmp_path):
    root = tmp_path / "content"
    (box / LEGACY_POINTER).write_text(str(root) + "\n", encoding="utf-8")
    before = cr.read_content_root()
    result = cr.migrate_legacy_config()
    assert before == str(root)
    assert result.migrated and result.value == str(root)
    assert registry_get(cr.CONTENT_ROOT_KEY) == str(root)
    assert (box / cr.POINTER_NAME).read_text(encoding="utf-8").strip() == str(root)
    assert (box / LEGACY_POINTER).exists()
    assert cr.read_content_root() == str(root)


def test_legacy_registry_key_only_is_migrated(box, tmp_path):
    root = tmp_path / "content"
    _registry(box, f"\"{LEGACY_KEY}\" = '{root}'\n")
    result = cr.migrate_legacy_config()
    assert result.migrated
    assert registry_get(cr.CONTENT_ROOT_KEY) == str(root)
    assert (box / cr.POINTER_NAME).is_file()
    assert LEGACY_KEY not in (box / "registry.local.toml").read_text(encoding="utf-8")


def test_engine_twin_is_migrated(box, tmp_path):
    root = tmp_path / "content"
    _registry(box, f"\"{LEGACY_ENGINE_KEY}\" = '{root}'\n")
    result = cr.migrate_legacy_config()
    assert cr.ENGINE_CONTENT_ROOT_KEY in result.written
    assert registry_get(cr.ENGINE_CONTENT_ROOT_KEY) == str(root)
    assert registry_get(cr.CONTENT_ROOT_KEY) == str(root)


def test_empty_content_root_with_legacy_value_resolves_and_migrates(box, tmp_path):
    root = tmp_path / "content"
    _registry(box, f"\"{cr.CONTENT_ROOT_KEY}\" = ''\n\"{LEGACY_KEY}\" = '{root}'\n")
    assert cr.read_content_root() == str(root)
    result = cr.migrate_legacy_config()
    assert result.migrated
    assert registry_get(cr.CONTENT_ROOT_KEY) == str(root)


def test_second_run_is_noop(box, tmp_path):
    (box / LEGACY_POINTER).write_text(str(tmp_path / "content"), encoding="utf-8")
    assert cr.migrate_legacy_config().migrated
    snapshot = {p.name: p.read_bytes() for p in box.iterdir()}
    second = cr.migrate_legacy_config()
    assert not second.migrated and second.written == ()
    assert {p.name: p.read_bytes() for p in box.iterdir()} == snapshot


def test_migration_spawns_no_process(box, tmp_path, monkeypatch):
    (box / LEGACY_POINTER).write_text(str(tmp_path / "content"), encoding="utf-8")

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert cr.migrate_legacy_config().migrated
