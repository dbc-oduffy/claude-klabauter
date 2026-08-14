"""
coordinator_core.install.test_sitepackages_pointer — Tier T coverage for the
site-packages pointer file `ensure_coordinator_venv` publishes at
`<settings-home>/bin/hook-sitepackages.txt` (2026-08-10-interpreter-surface-
four-asks.md chunk C5, ask (c)).

Purpose: pin the writer-side contract a sibling repo's hook bootstrap
already relies on -- exactly one absolute, existing path, written on every
REAL success exit (including the already-healthy fast path, the easiest one
to silently miss), and never written on a dry-run/check-only invocation.

Negative-spec:
  - Does NOT test reader-side validation (is-it-writable, is-it-under-an-
    expected-root) -- that trust boundary belongs to the sibling repo, not
    this writer (see ensure_venv.py's chunk-C5 docstring notes).
  - Does NOT exercise a real venv build/pip install -- monkeypatches
    `_venv_healthy`/`_site_packages_dir` the same way the rest of this
    module's test suite fakes venv mechanics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.install.ensure_venv as ensure_venv_mod
from coordinator_core.install.ensure_venv import (
    SITEPACKAGES_POINTER_NAME,
    ensure_coordinator_venv,
)


@pytest.fixture(autouse=True)
def _trust_any_root(monkeypatch):
    # Mirrors test_ensure_venv.py's fixture: bypasses
    # coordinator_trusted_root_guard so an arbitrary tmp_path plugin_root
    # can be used without registering it as a real coordinator/DoE/claude-klabauter
    # anchor.
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")


def _settings_home(tmp_path: Path) -> Path:
    sh = tmp_path / "settings-home"
    sh.mkdir()
    return sh


def _make_fake_site_packages(venv_dir: Path) -> Path:
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    return site_packages


def test_fast_path_already_healthy_writes_pointer(tmp_path, monkeypatch):
    """The already-healthy fast path (no lock, no rebuild) is the exit this
    chunk exists to cover -- it is the easiest of the real success exits to
    silently miss and the failure mode is invisible (no exception, no red
    test) if missed."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()
    site_packages = _make_fake_site_packages(venv_dir)

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)
    monkeypatch.setattr(ensure_venv_mod, "_site_packages_dir", lambda vd, is_win: site_packages)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test")

    assert result == "ready"
    pointer = settings_home / "bin" / SITEPACKAGES_POINTER_NAME
    assert pointer.is_file()
    lines = pointer.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0] == str(site_packages)
    assert Path(lines[0]).is_absolute()
    assert Path(lines[0]).exists()


def test_check_only_writes_nothing(tmp_path, monkeypatch):
    """A dry-run/check-only invocation must not mutate disk (AC: no pointer
    write on `would-rebuild`/`would-write`/check-only `ready`)."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()
    site_packages = _make_fake_site_packages(venv_dir)

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)
    monkeypatch.setattr(ensure_venv_mod, "_site_packages_dir", lambda vd, is_win: site_packages)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test", check_only=True)

    assert result == "ready"
    pointer = settings_home / "bin" / SITEPACKAGES_POINTER_NAME
    assert not pointer.exists()


def test_unresolvable_site_packages_skips_write_without_raising(tmp_path, monkeypatch):
    """A `None`/nonexistent derivation must SKIP the write, never publish a
    malformed pointer, and never fail venv provisioning itself."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)
    monkeypatch.setattr(ensure_venv_mod, "_site_packages_dir", lambda vd, is_win: None)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test")

    assert result == "ready"
    pointer = settings_home / "bin" / SITEPACKAGES_POINTER_NAME
    assert not pointer.exists()


def test_failed_publish_leaves_no_tmp_residue(tmp_path, monkeypatch):
    """A failed `os.replace` (simulated) must not leak a `.tmp-<pid>` file
    under `<settings-home>/bin/`, and must not raise into the caller --
    pins finding 1 (temp-file leak on failed publish) and finding 2
    (structural "never raises" via a broad `except Exception`)."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()
    site_packages = _make_fake_site_packages(venv_dir)

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)
    monkeypatch.setattr(ensure_venv_mod, "_site_packages_dir", lambda vd, is_win: site_packages)

    def _boom(*args, **kwargs):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(ensure_venv_mod.os, "replace", _boom)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test")

    assert result == "ready"
    bin_dir = settings_home / "bin"
    pointer = bin_dir / SITEPACKAGES_POINTER_NAME
    assert not pointer.exists()
    leftover = list(bin_dir.glob(f".{SITEPACKAGES_POINTER_NAME}.tmp-*"))
    assert leftover == []


def test_non_oserror_from_derivation_is_swallowed_and_warned(tmp_path, monkeypatch, capsys):
    """A non-`OSError` from the derivation path (e.g. a caller bug surfacing
    as a `TypeError`) must not propagate and must not publish a pointer --
    but unlike an `OSError`, it must emit a visible warning naming the
    swallowed exception type, since a silently-swallowed caller bug is
    exactly the failure mode chunk C5 exists to prevent (Review:
    coordinator:code-reviewer, e47656b7, finding 2 -- EM-overridden P2)."""
    settings_home = _settings_home(tmp_path)
    venv_dir = settings_home / ".coordinator-venv"
    venv_dir.mkdir()

    def _boom(vd, is_win):
        raise TypeError("simulated caller bug")

    monkeypatch.setattr(ensure_venv_mod, "_venv_healthy", lambda venv_py: True)
    monkeypatch.setattr(ensure_venv_mod, "_resolve_ml_cli", lambda plugin_root: None)
    monkeypatch.setattr(ensure_venv_mod, "_site_packages_dir", _boom)

    result = ensure_coordinator_venv(tmp_path, settings_home, site="test")

    assert result == "ready"
    pointer = settings_home / "bin" / SITEPACKAGES_POINTER_NAME
    assert not pointer.exists()
    err = capsys.readouterr().err
    assert "TypeError" in err
    assert "not published" in err
