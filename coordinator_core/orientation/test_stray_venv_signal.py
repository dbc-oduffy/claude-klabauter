"""
coordinator_core.orientation.test_stray_venv_signal — coverage for
`emit_stray_venvs`, which flags a `pyvenv.cfg` at a fixed set of conventional
venv locations under a fleet repo root, without walking the tree.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from coordinator_core.orientation import stray_venv_signal as sig


def _cfg(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")


def test_no_repo_root_renders_nothing(tmp_path: Path):
    assert sig.emit_stray_venvs(tmp_path / "does-not-exist") == ""


def test_clean_repo_renders_nothing(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    assert sig.emit_stray_venvs(tmp_path) == ""


def test_conventional_venv_at_root_is_reported(tmp_path: Path):
    _cfg(tmp_path / ".venv")
    out = sig.emit_stray_venvs(tmp_path)
    assert ".venv/pyvenv.cfg" in out


def test_pyvenv_cfg_at_repo_root_is_reported(tmp_path: Path):
    (tmp_path / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    assert "`pyvenv.cfg`" in sig.emit_stray_venvs(tmp_path)


def test_venv_one_level_down_is_reported(tmp_path: Path):
    _cfg(tmp_path / "tools" / "venv")
    assert "tools/venv/pyvenv.cfg" in sig.emit_stray_venvs(tmp_path)


def test_unconventional_name_is_not_reported(tmp_path: Path):
    _cfg(tmp_path / "fleet-env")
    assert sig.emit_stray_venvs(tmp_path) == ""


def test_vendor_git_and_node_modules_children_are_skipped(tmp_path: Path):
    for d in ("vendor", ".git", "node_modules"):
        _cfg(tmp_path / d / ".venv")
    assert sig.emit_stray_venvs(tmp_path) == ""


def test_deep_pyvenv_cfg_in_unlisted_directory_is_never_listed(tmp_path: Path, monkeypatch):
    deep = tmp_path / "scratch-hold" / "a" / "b"
    _cfg(deep / ".venv")
    listed: list[str] = []
    real_scandir = os.scandir

    def _spy(path="."):
        listed.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(sig, "os", SimpleNamespace(scandir=_spy))
    assert sig.emit_stray_venvs(tmp_path) == ""
    assert listed == [os.fspath(tmp_path)]


def test_enumeration_is_capped_but_count_is_exact(tmp_path: Path):
    for i in range(7):
        _cfg(tmp_path / f"d{i}" / ".venv")
    _cfg(tmp_path / "venv")
    out = sig.emit_stray_venvs(tmp_path)
    assert "8 `pyvenv.cfg`" in out
    assert "more" in out


def test_unreadable_path_fails_open(tmp_path: Path, monkeypatch):
    def _boom(*_a, **_kw):
        raise OSError("permission denied")

    # Swap the module's `os` binding, not `os.scandir` itself: patching the shared
    # `os` module breaks pytest's own tmp_path cleanup, which lists directories.
    monkeypatch.setattr(sig, "os", SimpleNamespace(scandir=_boom))
    assert sig.emit_stray_venvs(tmp_path) == ""
