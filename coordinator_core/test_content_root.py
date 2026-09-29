"""content_root resolution: new names first, legacy names as compat, installed plugin root for consumers."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import content_root as cr
from coordinator_core.hooks import block_unenumerated_agent_type as guard
from coordinator_core.machine_resolver import registry_get


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    settings = tmp_path / "settings"
    (settings / "machine-local").mkdir(parents=True)
    home.mkdir()
    for var in ("CLAUDE_PLUGIN_ROOT", "MACHINE_LOCAL_REGISTRY_DIR", "USERPROFILE", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    return home, settings


def _registry(settings: Path, body: str) -> None:
    (settings / "machine-local" / "registry.local.toml").write_text(body, encoding="utf-8")


def _plugin(home: Path, names=("executor",)) -> Path:
    root = home / ".claude" / "plugins" / "coordinator-claude"
    (root / "agents").mkdir(parents=True)
    for n in names:
        (root / "agents" / f"{n}.md").write_text(f"---\nname: {n}\n---\n", encoding="utf-8")
    (root / "subagent-sandbox-policy.yaml").write_text("report_sidecar: {}\n", encoding="utf-8")
    return root


def test_new_registry_key_wins_over_legacy(box):
    _, settings = box
    _registry(settings, '[repos]\ncontent_root = "/new"\ncontent_root = "/old"\n')
    assert cr.read_content_root() == "/new"


def test_legacy_registry_key_still_resolves(box):
    _, settings = box
    _registry(settings, '[repos]\ncontent_root = "/old"\n')
    assert cr.read_content_root() == "/old"


def test_registry_get_aliases_both_directions(box):
    _, settings = box
    _registry(settings, '[repos]\ncontent_root = "/new"\n')
    assert registry_get("repos.content_root") == "/new"
    _registry(settings, '[repos]\ncontent_root = "/old"\n')
    assert registry_get("repos.content_root") == "/old"


def test_new_pointer_file_beats_legacy_pointer(box):
    home, settings = box
    ml = settings / "machine-local"
    (ml / ".coordinator-content-root").write_text("/legacy\n")
    (home / ".claude").mkdir()
    (home / ".claude" / ".coordinator-content-root").write_text("/pointed\n")
    assert cr.read_content_root() == "/pointed"


def test_consumer_resolves_installed_plugin_root_with_no_key(box):
    home, _ = box
    root = _plugin(home)
    assert cr.read_content_root() == str(root)


def test_no_home_context_resolves_empty(monkeypatch, tmp_path):
    for var in ("CLAUDE_HOME", "HOME", "USERPROFILE", "CLAUDE_PLUGIN_ROOT", "COORDINATOR_SETTINGS_HOME", "MACHINE_LOCAL_REGISTRY_DIR"):
        monkeypatch.delenv(var, raising=False)
    assert cr.read_content_root() == ""


def test_agent_dispatch_roster_resolves_with_no_key(box):
    home, _ = box
    _plugin(home, names=("executor", "reviewer"))
    roster, reason = guard.resolve_roster(home=str(home))
    assert reason is None, reason
    assert {"coordinator:executor", "coordinator:reviewer"} <= set(roster)
