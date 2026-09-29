"""
coordinator_core.orientation.test_stray_venv_signal — coverage for
`emit_stray_venvs`, the orientation section flagging any `pyvenv.cfg` left
under a fleet repo root (a rogue venv's on-disk fingerprint).
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.orientation import stray_venv_signal as sig


def test_no_repo_root_renders_nothing(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert sig.emit_stray_venvs(missing) == ""


def test_clean_repo_renders_nothing(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    assert sig.emit_stray_venvs(tmp_path) == ""


def test_pyvenv_cfg_at_root_is_reported(tmp_path: Path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    out = sig.emit_stray_venvs(tmp_path)
    assert out != ""
    assert ".venv/pyvenv.cfg" in out


def test_nested_pyvenv_cfg_is_reported(tmp_path: Path):
    nested = tmp_path / "coordinator_core" / "fleet-env"
    nested.mkdir(parents=True)
    (nested / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    out = sig.emit_stray_venvs(tmp_path)
    assert "coordinator_core/fleet-env/pyvenv.cfg" in out


def test_vendor_path_segment_is_excluded(tmp_path: Path):
    vendored = tmp_path / "vendor" / "some-dep" / ".venv"
    vendored.mkdir(parents=True)
    (vendored / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    assert sig.emit_stray_venvs(tmp_path) == ""


def test_vendor_named_dir_without_segment_boundary_still_reports(tmp_path: Path):
    # "vendorized-tools" is NOT the path segment "vendor" -- must not be
    # swallowed by a substring-based exclusion.
    d = tmp_path / "vendorized-tools"
    d.mkdir()
    (d / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    out = sig.emit_stray_venvs(tmp_path)
    assert "vendorized-tools/pyvenv.cfg" in out


def test_git_and_node_modules_are_pruned(tmp_path: Path):
    for d in (".git", "node_modules"):
        p = tmp_path / d / "sub"
        p.mkdir(parents=True)
        (p / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    assert sig.emit_stray_venvs(tmp_path) == ""


def test_enumeration_is_capped_but_count_is_exact(tmp_path: Path):
    for i in range(8):
        d = tmp_path / f"env{i}"
        d.mkdir()
        (d / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    out = sig.emit_stray_venvs(tmp_path)
    assert "8 `pyvenv.cfg`" in out
    assert "more" in out


def test_unreadable_path_fails_open(tmp_path: Path, monkeypatch):
    def _boom(*_a, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr(sig.os, "walk", _boom)
    assert sig.emit_stray_venvs(tmp_path) == ""
