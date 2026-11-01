"""claude-author reads `coordinator.plugin_root` ahead of `<clone>/coordinator`.

Why: the engine-off-E cutover loads hooks and skills from a synced C: copy of the
plugin while `.coordinator-content-root` and `repos.content_root` stay on the E: dev clone. A stale
override (no payload marker) must fall back, never fail the launch.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def claude_author():
    spec = importlib.util.spec_from_file_location("_claude_author_plugin_root", _BIN_DIR / "claude-author.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _registry(tmp_path: Path, value: str) -> Path:
    ml = tmp_path / "machine-local"
    ml.mkdir()
    (ml / "registry.local.toml").write_text(f'"coordinator.plugin_root" = \'{value}\'\n', encoding="utf-8")
    return ml


def test_override_wins_when_it_carries_the_payload(tmp_path, monkeypatch, claude_author):
    plugin = tmp_path / "c-copy" / "coordinator"
    (plugin / "templates" / "bin").mkdir(parents=True)
    (plugin / "templates" / "bin" / "_machine_local.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(_registry(tmp_path, str(plugin))))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert claude_author._plugin_root_override() == str(plugin)


def test_override_without_payload_is_ignored(tmp_path, monkeypatch, claude_author):
    empty = tmp_path / "stale"
    empty.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(_registry(tmp_path, str(empty))))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert claude_author._plugin_root_override() is None


def test_absent_key_is_none(tmp_path, monkeypatch, claude_author):
    ml = tmp_path / "machine-local"
    ml.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert claude_author._plugin_root_override() is None
