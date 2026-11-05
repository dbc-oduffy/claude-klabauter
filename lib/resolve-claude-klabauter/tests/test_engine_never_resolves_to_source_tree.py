"""
No rung of ``resolve_claude_klabauter_root_with_class`` returns a claude-klabauter tree.

PM ruling 2026-11-05: "I never want claude-klabauter to be used as the engine. Ever."
A sibling claude-klabauter tree, a registered ``repos.claude_klabauter`` and a
``COORDINATOR_ENGINE_ROOT`` at a claude-klabauter tree each fail loudly, naming how to
fetch claude-klabauter, when no stamped published engine exists. With one, the
published engine answers and the claude-klabauter tree is ignored. The LOCATOR
(``_resolve_claude_klabauter_root``) still returns the live tree by design.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SHIM_PATH = Path(__file__).resolve().parent.parent / "_resolve_claude_klabauter.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location("_never_claude_klabauter_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path, *, stamped: bool) -> Path:
    (root / "coordinator_core").mkdir(parents=True)
    if stamped:
        (root / "coordinator_core" / "_engine_stamp").write_text("sha:test\n", encoding="utf-8")
    return root


@pytest.fixture
def box(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    shim = _load_shim()
    monkeypatch.setattr(shim, "_self_located_root", lambda: None)
    return tmp_path, ml_dir, shim


def _registry(ml_dir: Path, **repos: Path) -> None:
    ml_dir.joinpath("registry.local.toml").write_text(
        "".join(f'"repos.{k}" = \'{v.as_posix()}\'\n' for k, v in repos.items()),
        encoding="utf-8",
    )


def _assert_fails_naming_fetch(shim) -> None:
    assert shim.resolve_claude_klabauter_root_with_class() == (None, shim.RESOLUTION_UNRESOLVED)
    msg = shim.unresolved_engine_message()
    assert "https://github.com/dbc-oduffy/claude-klabauter" in msg
    assert "live-working-tree" not in msg


def test_sibling_claude_klabauter_tree_alone_fails_loudly(box, monkeypatch):
    tmp_path, ml_dir, shim = box
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    monkeypatch.setattr(shim, "_self_located_root", lambda: live.as_posix())

    _assert_fails_naming_fetch(shim)


def test_registered_claude_klabauter_alone_fails_loudly(box):
    tmp_path, ml_dir, shim = box
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    _registry(ml_dir, claude_klabauter=live)

    _assert_fails_naming_fetch(shim)


def test_env_override_at_claude_klabauter_tree_alone_fails_loudly(box, monkeypatch):
    tmp_path, ml_dir, shim = box
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(live))

    _assert_fails_naming_fetch(shim)


def test_env_override_at_claude_klabauter_tree_loses_to_published_engine(box, monkeypatch):
    tmp_path, ml_dir, shim = box
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    klabauter = _tree(tmp_path / "claude-klabauter", stamped=True)
    _registry(ml_dir, **{"claude_klabauter": klabauter, "claude_klabauter": live})
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(live))

    assert shim.resolve_claude_klabauter_root_with_class() == (
        klabauter.as_posix(),
        shim.RESOLUTION_RESOLVED_ENGINE,
    )


def test_nonexistent_env_override_falls_through_to_published_engine(box, monkeypatch):
    tmp_path, ml_dir, shim = box
    klabauter = _tree(tmp_path / "claude-klabauter", stamped=True)
    _registry(ml_dir, claude_klabauter=klabauter)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tmp_path / "does-not-exist"))

    assert shim.resolve_claude_klabauter_root_with_class() == (
        klabauter.as_posix(),
        shim.RESOLUTION_RESOLVED_ENGINE,
    )


def test_stamped_env_override_is_honoured(box, monkeypatch):
    tmp_path, ml_dir, shim = box
    stamped = _tree(tmp_path / "other-engine", stamped=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(stamped))

    assert shim.resolve_claude_klabauter_root_with_class() == (
        str(stamped),
        shim.RESOLUTION_RESOLVED_ENGINE,
    )


def test_locator_still_returns_live_tree(box):
    tmp_path, ml_dir, shim = box
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    _registry(ml_dir, claude_klabauter=live)

    assert shim._resolve_claude_klabauter_root(ml_dir) == live.as_posix()
