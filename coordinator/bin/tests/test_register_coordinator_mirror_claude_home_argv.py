"""test_register_coordinator_mirror_claude_home_argv.py — pytest coverage for
`coordinator/lib/register-coordinator-mirror.py::_claude_home_argv`.

Review: code-reviewer (F1, P1) — this helper had zero test coverage anywhere
in the repo despite carrying the same settings-home-first Windows `.cmd`
precedence fix as `maximalist.py::_claude_home_cli_argv` (which DOES have
existing coverage, extended in this same review pass — see
`coordinator_core/install/test_maximalist.py`). Both-candidates-present is
the case that actually pins precedence; a test creating only one candidate
cannot detect an inverted resolution order.

Spec backlink: coordinator/lib/register-coordinator-mirror.py::_claude_home_argv
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "lib", "register-coordinator-mirror.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("register_coordinator_mirror", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def test_claude_home_argv_posix_bare_name(mod, monkeypatch):
    monkeypatch.setattr(mod.os, "name", "posix")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod._claude_home_argv("plugins") == ["claude-home", "plugins"]


def test_claude_home_argv_windows_mirror_only(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cand = tmp_path / ".claude" / "bin" / "claude-home.cmd"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod._claude_home_argv("plugins") == [str(cand), "plugins"]


def test_claude_home_argv_windows_settings_home_only(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cand = tmp_path / ".coordinator-claude-settings" / "bin" / "claude-home.cmd"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod._claude_home_argv("plugins") == [str(cand), "plugins"]


def test_claude_home_argv_windows_both_present_settings_home_wins(mod, tmp_path, monkeypatch):
    """Precedence pin: both candidates on disk, settings-home must win."""
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    settings_home_cand = tmp_path / ".coordinator-claude-settings" / "bin" / "claude-home.cmd"
    mirror_cand = tmp_path / ".claude" / "bin" / "claude-home.cmd"
    for cand in (settings_home_cand, mirror_cand):
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod._claude_home_argv("plugins") == [str(settings_home_cand), "plugins"]


def test_claude_home_argv_windows_neither_present_falls_back_to_path_lookup(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(mod.shutil, "which", lambda name: "C:\\PATH\\claude-home.cmd")
    assert mod._claude_home_argv("plugins") == ["C:\\PATH\\claude-home.cmd", "plugins"]


def test_claude_home_argv_windows_neither_present_nor_on_path_falls_back_to_bare_name(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod._claude_home_argv("plugins") == ["claude-home", "plugins"]
    assert "claude-home not found" in capsys.readouterr().err
