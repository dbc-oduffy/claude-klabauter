"""
A registered, stamped published engine outranks every ambient claude-klabauter rung.

PM ruling 2026-11-05: "claude-klabauter running the live code is suicidal". The dispatch
ladder (``resolve_claude_klabauter_root_with_class``) used to divert to the published
engine only when ``engine.target`` was readable AND the session sat outside the
live tree; a box missing either fact ran the live tree. Both conjuncts are gone.

Pins both sides: a stamped registered engine wins with ``engine.target`` unset,
with the session inside the live tree, and with ``repos.claude_klabauter``
registered; an unstamped or unregistered engine fails loudly (never the live tree).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SHIM_PATH = Path(__file__).resolve().parent.parent / "_resolve_claude_klabauter.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location("_ladder_outranks_shim", _SHIM_PATH)
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


def test_stamped_engine_wins_with_engine_target_unset_and_live_tree_registered(box):
    tmp_path, ml_dir, shim = box
    klabauter = _tree(tmp_path / "claude-klabauter", stamped=True)
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    _registry(ml_dir, **{"claude_klabauter": klabauter, "claude_klabauter": live})

    assert shim.resolve_engine_target(ml_dir) is None
    root, cls = shim.resolve_claude_klabauter_root_with_class()

    assert (root, cls) == (klabauter.as_posix(), shim.RESOLUTION_RESOLVED_ENGINE)


def test_stamped_engine_wins_when_session_is_inside_the_live_tree(box, monkeypatch):
    tmp_path, ml_dir, shim = box
    klabauter = _tree(tmp_path / "claude-klabauter", stamped=True)
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    _registry(ml_dir, **{"claude_klabauter": klabauter, "claude_klabauter": live})
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(live))

    assert shim._is_claude_klabauter_source_tree(ml_dir) is True
    root, cls = shim.resolve_claude_klabauter_root_with_class()

    assert (root, cls) == (klabauter.as_posix(), shim.RESOLUTION_RESOLVED_ENGINE)


def test_unstamped_engine_fails_loudly_instead_of_falling_to_live_tree(box):
    tmp_path, ml_dir, shim = box
    klabauter = _tree(tmp_path / "claude-klabauter", stamped=False)
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    _registry(ml_dir, **{"claude_klabauter": klabauter, "claude_klabauter": live})

    assert shim.resolve_claude_klabauter_root_with_class() == (None, shim.RESOLUTION_UNRESOLVED)


def test_no_registered_engine_fails_loudly_instead_of_falling_to_live_tree(box):
    tmp_path, ml_dir, shim = box
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    _registry(ml_dir, claude_klabauter=live)

    assert shim.resolve_claude_klabauter_root_with_class() == (None, shim.RESOLUTION_UNRESOLVED)


def test_locator_axis_still_returns_live_tree_when_engine_registered(box):
    tmp_path, ml_dir, shim = box
    klabauter = _tree(tmp_path / "claude-klabauter", stamped=True)
    live = _tree(tmp_path / "claude-klabauter", stamped=False)
    (live / "coordinator" / "bin").mkdir(parents=True)
    (live / "coordinator" / "bin" / "archive-stamp-cli.py").write_text("", encoding="utf-8")
    _registry(ml_dir, **{"claude_klabauter": klabauter, "claude_klabauter": live})

    assert Path(shim.resolve_claude_klabauter_bin_dir()) == live / "coordinator" / "bin"
