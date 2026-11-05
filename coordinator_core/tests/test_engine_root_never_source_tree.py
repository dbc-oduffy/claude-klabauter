"""coordinator_engine_root_with_class never returns a claude-klabauter tree (PM ruling 2026-11-05)."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import engine_root as engine_root_mod


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    for var in ("CLAUDE_KLABAUTER_ROOT", "COORDINATOR_ENGINE_ROOT", "CLAUDE_PROJECT_DIR",
                "MACHINE_LOCAL_REGISTRY_DIR", "CLAUDE_KLABAUTER_ROOT_SKEW_QUIET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "1")
    engine_root_mod._reset_shim_cache()
    engine_root_mod._reset_gate_memo()
    yield
    engine_root_mod._reset_shim_cache()
    engine_root_mod._reset_gate_memo()


def _registry(tmp_path: Path, body: str) -> None:
    (tmp_path / "settings-home" / "machine-local" / "registry.local.toml").write_text(
        body, encoding="utf-8"
    )


def _stamped(root: Path) -> Path:
    stamp = root / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("sha:test\n", encoding="utf-8")
    return root


def _claude_klabauter_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "claude-klabauter"
    (tree / "coordinator_core").mkdir(parents=True)
    return tree


def test_env_override_at_claude_klabauter_tree_without_published_engine_is_unresolved(tmp_path, monkeypatch):
    tree = _claude_klabauter_tree(tmp_path)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tree))
    shim = engine_root_mod._load_shim()

    assert engine_root_mod.coordinator_engine_root_with_class() == (None, "unresolved")
    assert "github.com/dbc-oduffy/claude-klabauter" in shim.unresolved_engine_message()


def test_registered_claude_klabauter_tree_without_published_engine_is_unresolved(tmp_path):
    tree = _claude_klabauter_tree(tmp_path)
    _registry(tmp_path, f'[repos]\nclaude_klabauter = "{tree.as_posix()}"\n')

    assert engine_root_mod.coordinator_engine_root_with_class() == (None, "unresolved")


def test_unresolved_matches_the_shim_class_api(tmp_path):
    shim = engine_root_mod._load_shim()

    assert shim.resolve_claude_klabauter_root_with_class() == (None, "unresolved")
    assert engine_root_mod.coordinator_engine_root_with_class() == (None, "unresolved")


def test_env_override_at_claude_klabauter_tree_falls_through_to_published_engine(tmp_path, monkeypatch):
    tree = _claude_klabauter_tree(tmp_path)
    published = _stamped(tmp_path / "klabauter")
    _registry(tmp_path, f'[repos]\nclaude_klabauter = "{published.as_posix()}"\n')
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tree))

    root, cls = engine_root_mod.coordinator_engine_root_with_class()

    assert Path(root) == published
    assert cls == "resolved-engine"


def test_nonexistent_env_override_falls_through_to_published_engine(tmp_path, monkeypatch):
    published = _stamped(tmp_path / "klabauter")
    _registry(tmp_path, f'[repos]\nclaude_klabauter = "{published.as_posix()}"\n')
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tmp_path / "does-not-exist"))

    root, cls = engine_root_mod.coordinator_engine_root_with_class()

    assert Path(root) == published
    assert cls == "resolved-engine"


def test_stamped_env_override_is_honoured(tmp_path, monkeypatch):
    other = _stamped(tmp_path / "stamped-engine")
    published = _stamped(tmp_path / "klabauter")
    _registry(tmp_path, f'[repos]\nclaude_klabauter = "{published.as_posix()}"\n')
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(other))

    root, _cls = engine_root_mod.coordinator_engine_root_with_class()

    assert Path(root) == other


def test_published_engine_outranks_sibling_claude_klabauter_registration(tmp_path):
    tree = _claude_klabauter_tree(tmp_path)
    published = _stamped(tmp_path / "klabauter")
    _registry(
        tmp_path,
        f'[repos]\nclaude_klabauter = "{tree.as_posix()}"\n'
        f'claude_klabauter = "{published.as_posix()}"\n',
    )

    root, cls = engine_root_mod.coordinator_engine_root_with_class()

    assert Path(root) == published
    assert cls == "resolved-engine"
