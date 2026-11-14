"""Tests for the doctor's engine-site currency layer.

The interpreter path is injected through monkeypatched `shutil.which` / `os.path.realpath`,
never a real symlink, so the file runs on Windows without symlink privilege.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops import doctor


@pytest.fixture
def bump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Settings home stamped 3.13 with yaml; python3 resolves to a 3.15 interpreter."""
    home = tmp_path / "settings-home"
    site = home / "engine-site"
    (site / "yaml").mkdir(parents=True)
    (site / ".engine-site-stamp").write_text("3.13\nabc\n")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))

    prefix = tmp_path / "brew" / "3.15"
    (prefix / "bin").mkdir(parents=True)
    interp = prefix / "bin" / "python3.15"
    interp.write_text("")
    managed_lib = prefix / "lib" / "python3.15"
    managed_lib.mkdir(parents=True)

    real_realpath = os.path.realpath

    def fake_realpath(p, *a, **k):
        return str(interp) if str(p) == "python3-link" else real_realpath(p, *a, **k)

    monkeypatch.setattr(doctor.shutil, "which", lambda n, *a, **k: "python3-link" if n == "python3" else None)
    monkeypatch.setattr(os.path, "realpath", fake_realpath)
    return managed_lib


def test_bump_on_externally_managed_interpreter_is_broken_and_names_fix(bump: Path):
    (bump / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
    layer = doctor._check_engine_site_currency()
    assert layer.status == "broken"
    text = layer.findings[0].message
    assert "3.15" in text and "3.13" in text and "python3 scripts/setup.py" in text


def test_bump_on_unguarded_interpreter_is_ok(bump: Path):
    assert doctor._check_engine_site_currency().status == "ok"


def test_current_engine_site_is_ok(bump: Path, tmp_path: Path):
    (bump / "EXTERNALLY-MANAGED").write_text("")
    (tmp_path / "settings-home" / "engine-site" / ".engine-site-stamp").write_text("3.15\nabc\n")
    assert doctor._check_engine_site_currency().status == "ok"


def test_unreadable_minor_is_not_a_silent_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / "python.exe"
    exe.write_text("")
    (tmp_path / "lib" / "python3.12").mkdir(parents=True)
    (tmp_path / "lib" / "python3.12" / "EXTERNALLY-MANAGED").write_text("")
    monkeypatch.setattr(doctor.shutil, "which", lambda n, *a, **k: str(exe) if n == "python" else None)
    layer = doctor._check_engine_site_currency()
    assert layer.status == "unknown"
    assert str(exe) in layer.findings[0].message


def test_unreadable_minor_on_an_unmanaged_interpreter_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Windows `python.exe` is not PEP 668 and needs no engine-site; it must not exit the doctor 1."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / "python.exe"
    exe.write_text("")
    monkeypatch.setattr(doctor.shutil, "which", lambda n, *a, **k: str(exe) if n == "python" else None)
    assert doctor._check_engine_site_currency().status == "ok"


def test_no_python_on_path_is_unknown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda *a, **k: None)
    assert doctor._check_engine_site_currency().status == "unknown"
