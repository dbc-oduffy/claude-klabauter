
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "register-coordinator-mirror.py"
_spec = importlib.util.spec_from_file_location("register_coordinator_mirror_lib", _MODULE_PATH)
register_coordinator_mirror = importlib.util.module_from_spec(_spec)
sys.modules["register_coordinator_mirror_lib"] = register_coordinator_mirror
_spec.loader.exec_module(register_coordinator_mirror)


def test_windows_prefers_settings_home_candidate_when_both_present(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    settings_home_cand = tmp_path / ".coordinator-claude-settings" / "bin" / "claude-home.cmd"
    mirror_cand = tmp_path / ".claude" / "bin" / "claude-home.cmd"
    for cand in (settings_home_cand, mirror_cand):
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(register_coordinator_mirror.shutil, "which", lambda name: None)
    assert register_coordinator_mirror._claude_home_argv("plugins") == [
        str(settings_home_cand),
        "plugins",
    ]


def test_windows_falls_back_to_mirror_when_settings_home_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    mirror_cand = tmp_path / ".claude" / "bin" / "claude-home.cmd"
    mirror_cand.parent.mkdir(parents=True, exist_ok=True)
    mirror_cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(register_coordinator_mirror.shutil, "which", lambda name: None)
    assert register_coordinator_mirror._claude_home_argv("plugins") == [
        str(mirror_cand),
        "plugins",
    ]


def test_windows_falls_back_to_path_lookup_when_neither_candidate_present(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(
        register_coordinator_mirror.shutil, "which", lambda name: "C:\\PATH\\claude-home.cmd"
    )
    assert register_coordinator_mirror._claude_home_argv("plugins") == [
        "C:\\PATH\\claude-home.cmd",
        "plugins",
    ]


def test_posix_uses_bare_name(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "posix")
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(register_coordinator_mirror.shutil, "which", lambda name: None)
    assert register_coordinator_mirror._claude_home_argv("plugins") == ["claude-home", "plugins"]


def test_posix_prefers_settings_home_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "posix")
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cand = tmp_path / ".coordinator-claude-settings" / "bin" / "claude-home"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(register_coordinator_mirror.shutil, "which", lambda name: None)
    assert register_coordinator_mirror._claude_home_argv("plugins") == [str(cand), "plugins"]


def test_posix_falls_back_to_mirror_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "posix")
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cand = tmp_path / ".claude" / "bin" / "claude-home"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(register_coordinator_mirror.shutil, "which", lambda name: None)
    assert register_coordinator_mirror._claude_home_argv("plugins") == [str(cand), "plugins"]


def test_posix_path_lookup_outranks_the_retired_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "posix")
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    mirror_cand = tmp_path / ".claude" / "bin" / "claude-home"
    mirror_cand.parent.mkdir(parents=True, exist_ok=True)
    mirror_cand.write_text("", encoding="utf-8")
    on_path = tmp_path / "elsewhere" / "claude-home"
    on_path.parent.mkdir(parents=True, exist_ok=True)
    on_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(register_coordinator_mirror.shutil, "which", lambda name: str(on_path))
    assert register_coordinator_mirror._claude_home_argv("plugins") == [str(on_path), "plugins"]


def test_posix_both_candidates_present_settings_home_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(register_coordinator_mirror.os, "name", "posix")
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    settings_home_cand = tmp_path / ".coordinator-claude-settings" / "bin" / "claude-home"
    mirror_cand = tmp_path / ".claude" / "bin" / "claude-home"
    for cand in (settings_home_cand, mirror_cand):
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(register_coordinator_mirror.shutil, "which", lambda name: None)
    assert register_coordinator_mirror._claude_home_argv("plugins") == [
        str(settings_home_cand),
        "plugins",
    ]
