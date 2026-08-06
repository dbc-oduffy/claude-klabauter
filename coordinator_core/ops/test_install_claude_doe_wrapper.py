"""Characterization tests for coordinator_core.ops.install_claude_doe_wrapper.

Port source: coordinator/commands/install.md (example-doctrine-repo) Step 3.5b, the
literal bash fence at line 892.
Spec backlink: docs/plans/2026-07-23-skills-carry-no-code-extirpation.md § M3/D9
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from coordinator_core.ops.install_claude_doe_wrapper import main


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PATH", "")


def _make_wrapper_src(tmp_path: Path) -> Path:
    src = tmp_path / "claude-doe"
    src.write_text("#!/bin/sh\nexec claude \"$@\"\n")
    src.chmod(src.stat().st_mode | stat.S_IEXEC)
    return src


def test_missing_wrapper_src_flag_fails_loud(capsys):
    rc = main([])
    assert rc == 1
    assert "claude_doe_wrapper: failed (--wrapper-src not supplied)" in capsys.readouterr().out


def test_wrapper_src_not_found_fails_loud(tmp_path, capsys):
    rc = main(["--wrapper-src", str(tmp_path / "nope")])
    assert rc == 1
    assert "claude_doe_wrapper: failed (wrapper source not found" in capsys.readouterr().out


def test_fresh_install_copies_and_chmods(tmp_path, monkeypatch, capsys):
    src = _make_wrapper_src(tmp_path)
    home = Path(os.environ["HOME"])

    rc = main(["--wrapper-src", str(src)])

    assert rc == 0
    dst = home / ".local" / "bin" / "claude-doe"
    assert dst.is_file()
    assert dst.stat().st_mode & stat.S_IEXEC
    assert dst.read_text() == src.read_text()
    out = capsys.readouterr().out
    assert f"claude_doe_wrapper: installed ({dst})" in out
    assert "NOTE:" in out  # ~/.local/bin is not on PATH in this sandboxed test


def test_claude_home_override_used_for_dest(tmp_path, monkeypatch, capsys):
    src = _make_wrapper_src(tmp_path)
    claude_home = tmp_path / "alt-claude-home"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    rc = main(["--wrapper-src", str(src)])

    assert rc == 0
    dst = claude_home / ".local" / "bin" / "claude-doe"
    assert dst.is_file()


def test_check_only_fails_loud_when_absent(tmp_path, capsys):
    src = _make_wrapper_src(tmp_path)
    home = Path(os.environ["HOME"])

    rc = main(["--check-only", "--wrapper-src", str(src)])

    assert rc == 1
    dst = home / ".local" / "bin" / "claude-doe"
    assert not dst.exists()
    out = capsys.readouterr().out
    assert f"claude_doe_wrapper: failed (absent: {dst})" in out


def test_check_only_fails_loud_when_stale(tmp_path, capsys):
    src = _make_wrapper_src(tmp_path)
    home = Path(os.environ["HOME"])

    assert main(["--wrapper-src", str(src)]) == 0
    capsys.readouterr()

    # Mutate the source after install so dest content diverges.
    src.write_text(src.read_text() + "\n# changed\n")

    rc = main(["--check-only", "--wrapper-src", str(src)])
    assert rc == 1
    dst = home / ".local" / "bin" / "claude-doe"
    out = capsys.readouterr().out
    assert f"claude_doe_wrapper: failed (stale: {dst})" in out


def test_check_only_reports_ready_when_already_installed(tmp_path, capsys):
    src = _make_wrapper_src(tmp_path)
    home = Path(os.environ["HOME"])

    assert main(["--wrapper-src", str(src)]) == 0
    capsys.readouterr()

    rc = main(["--check-only", "--wrapper-src", str(src)])
    assert rc == 0
    dst = home / ".local" / "bin" / "claude-doe"
    assert f"claude_doe_wrapper: ready ({dst})" in capsys.readouterr().out


def test_no_note_when_local_bin_already_on_path(tmp_path, monkeypatch, capsys):
    src = _make_wrapper_src(tmp_path)
    home = Path(os.environ["HOME"])
    local_bin = home / ".local" / "bin"
    monkeypatch.setenv("PATH", str(local_bin))

    rc = main(["--wrapper-src", str(src)])

    assert rc == 0
    assert "NOTE:" not in capsys.readouterr().out
