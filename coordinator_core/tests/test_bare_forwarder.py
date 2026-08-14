"""Tests for coordinator_core.bare_forwarder.forward.

Gate 1 coverage (settings-home-first rung): forward() now tries
``<settings-home>/bin/<name>`` before falling back to the legacy
``.claude/bin/<name>`` rung under CLAUDE_HOME (or under the home directory when
CLAUDE_HOME is unset), mirroring the precedence
used by ``coordinator_core.pyresolve._machine_local_impl``.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292
    (T1b chunk B2 — bare forwarders: claude-home, coordinator-settings-home)

The launchability seam stubbed here is `bare_forwarder.is_executable`, NOT
`os.access`: `forward()` resolves through `win_portability.is_executable`,
which is mode-bit inspection on POSIX and PATHEXT-sibling resolution on
Windows — it never calls `os.access`, and an extensionless `widget` fixture
is correctly non-launchable on Windows. Stubbing `os.access` pinned a
predicate production does not use and only ever passed on POSIX.
"""
from __future__ import annotations

import os

import pytest

from coordinator_core import bare_forwarder


def test_falls_back_to_legacy_when_settings_home_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    legacy = tmp_path / ".claude" / "bin" / "widget"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\necho hi\n")
    legacy.chmod(0o755)

    monkeypatch.setattr(
        bare_forwarder, "is_executable", lambda path: str(path) == str(legacy)
    )

    captured = {}

    def fake_execv(path, args):
        captured["path"] = path
        captured["args"] = args
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)

    with pytest.raises(SystemExit) as exc_info:
        bare_forwarder.forward("widget", ["--help"])

    assert exc_info.value.code == 0
    assert captured["path"] == str(legacy)
    assert captured["args"] == [str(legacy), "--help"]


def test_falls_back_to_legacy_via_userprofile_when_home_absent(tmp_path, monkeypatch):
    """Native-Windows condition (home-resolution-lint bare_home_or_chain fix,
    2026-07-29): CLAUDE_HOME/HOME both absent. `forward()`'s legacy candidate
    now resolves via `_settings_home.home_dir()` instead of a hand-rolled
    `CLAUDE_HOME or HOME` chain that degraded to a cwd-relative
    `.claude/bin/widget` in exactly this condition."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    userprofile_home = tmp_path / "winhome"
    legacy = userprofile_home / ".claude" / "bin" / "widget"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\necho hi\n")
    legacy.chmod(0o755)
    # Path.home() only consults USERPROFILE on a real Windows interpreter;
    # simulate that resolution here so the test proves the delegation shape.
    monkeypatch.setattr(bare_forwarder, "home_dir", lambda: userprofile_home)

    monkeypatch.setattr(
        bare_forwarder, "is_executable", lambda path: str(path) == str(legacy)
    )

    captured = {}

    def fake_execv(path, args):
        captured["path"] = path
        captured["args"] = args
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)

    with pytest.raises(SystemExit) as exc_info:
        bare_forwarder.forward("widget", ["--help"])

    assert exc_info.value.code == 0
    assert captured["path"] == str(legacy)


def test_resolves_settings_home_first_even_when_legacy_present(tmp_path, monkeypatch):
    settings_home_dir = tmp_path / "settings-home"
    settings_home_bin = settings_home_dir / "bin" / "widget"
    settings_home_bin.parent.mkdir(parents=True)
    settings_home_bin.write_text("#!/bin/sh\necho settings-home\n")
    settings_home_bin.chmod(0o755)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))

    claude_home = tmp_path / "claude-home"
    legacy = claude_home / ".claude" / "bin" / "widget"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\necho legacy\n")
    legacy.chmod(0o755)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    monkeypatch.setattr(
        bare_forwarder,
        "is_executable",
        lambda path: str(path) in {str(settings_home_bin), str(legacy)},
    )

    captured = {}

    def fake_execv(path, args):
        captured["path"] = path
        captured["args"] = args
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)

    with pytest.raises(SystemExit) as exc_info:
        bare_forwarder.forward("widget", ["get", "repos.foo"])

    assert exc_info.value.code == 0
    assert captured["path"] == str(settings_home_bin)
    assert captured["args"] == [str(settings_home_bin), "get", "repos.foo"]


def test_exits_127_when_neither_rung_executable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(bare_forwarder, "is_executable", lambda path: False)

    with pytest.raises(SystemExit) as exc_info:
        bare_forwarder.forward("widget", [])

    assert exc_info.value.code == 127
    err = capsys.readouterr().err
    assert "widget: resolver not installed at" in err
    assert "run /coordinator:setup (Phase 3)." in err
