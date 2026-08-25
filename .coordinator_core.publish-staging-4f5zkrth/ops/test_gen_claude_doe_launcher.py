"""Characterization + parity tests for coordinator_core.ops.gen_claude_doe_launcher.

Spec backlink: tasks/2026-07-14-install-dogfood-friction.md section F5
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops import gen_claude_doe_launcher
from coordinator_core.ops.gen_claude_doe_launcher import is_windows, main


def _make_template_dir(tmp_path: Path, cmd_lines: int = 3, ps1_lines: int = 3) -> Path:
    tmpl_dir = tmp_path / "templates" / "bin"
    tmpl_dir.mkdir(parents=True)
    (tmpl_dir / "claude-doe-launcher.cmd.tmpl").write_text(
        "\n".join(f"rem line {i}" for i in range(cmd_lines)) + "\n"
    )
    (tmpl_dir / "claude-doe-launcher.ps1.tmpl").write_text(
        "\n".join(f"# line {i}" for i in range(ps1_lines)) + "\n"
    )
    return tmpl_dir


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets its own CLAUDE_HOME sandbox; no test touches the real launchers."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.delenv("OS", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    return home


def _force_windows(monkeypatch):
    monkeypatch.setenv("MSYSTEM", "MINGW64")


# ---------------------------------------------------------------------------
# Windows-gate: non-Windows is a clean no-op
# ---------------------------------------------------------------------------


def test_non_windows_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.delenv("OS", raising=False)
    # is_windows()'s third signal shells out to `uname -s` when it is on
    # PATH -- on a Git-Bash host (this dev box) or a native win32 test run
    # with a `uname` shim on PATH, that reports MINGW*/MSYS*, so clearing
    # MSYSTEM/OS alone is not sufficient to simulate "non-Windows" here;
    # the mocked-foreign-platform test must neutralize the real seam, not
    # just the two env vars. gen_claude_doe_launcher.is_windows() shells
    # out via `shutil.which`, so patch that seam directly.
    monkeypatch.setattr(
        gen_claude_doe_launcher.shutil, "which",
        lambda name, *a, **kw: None if name == "uname" else __import__("shutil").which(name),
    )
    tmpl_dir = _make_template_dir(tmp_path)
    # --no-smoke: belt-and-braces -- without the flag the post-render smoke
    # would execute the rendered launcher (and the real `claude`) from a
    # pytest tmp dir, a side effect no unit test may have. The flag does
    # not weaken the assertions below.
    rc = main(["--template-dir", str(tmpl_dir), "--no-smoke"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "not a Windows environment" in err
    home = Path(os.environ["CLAUDE_HOME"])
    assert not (home / ".local" / "bin" / "claude-doe.cmd").exists()


def test_windows_nt_env_signal_detected(monkeypatch):
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.setenv("OS", "Windows_NT")
    assert is_windows() is True


def test_msystem_signal_detected(monkeypatch):
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    assert is_windows() is True


# ---------------------------------------------------------------------------
# CLI arg parsing
# ---------------------------------------------------------------------------


def test_help_exits_zero(capsys):
    rc = main(["--help"])
    assert rc == 0
    assert "Usage: gen-claude-doe-launcher.sh" in capsys.readouterr().err


def test_unknown_argument_exits_one(capsys):
    rc = main(["--bogus"])
    assert rc == 1
    assert "Unknown argument: --bogus" in capsys.readouterr().err


def test_template_dir_missing_value_exits_one(capsys):
    """Negative-spec: oracle crashes on `set -u` unbound variable (exit 1, no write).
    This port reproduces the outward contract (exit 1, clean stderr) without the
    literal bash traceback text."""
    rc = main(["--template-dir"])
    assert rc == 1
    assert capsys.readouterr().err  # some diagnostic emitted


# ---------------------------------------------------------------------------
# Windows render path
# ---------------------------------------------------------------------------


def test_missing_template_dir_arg_is_fail_loud(monkeypatch, capsys):
    _force_windows(monkeypatch)
    rc = main([])
    assert rc == 1
    assert "--template-dir not supplied" in capsys.readouterr().err


def test_template_dir_not_found_exits_one(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    rc = main(["--template-dir", str(tmp_path / "nope")])
    assert rc == 1
    assert "Template not found" in capsys.readouterr().err


def test_missing_cmd_template_exits_one(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    tmpl_dir = tmp_path / "partial"
    tmpl_dir.mkdir()
    (tmpl_dir / "claude-doe-launcher.ps1.tmpl").write_text("# ps1\n")
    rc = main(["--template-dir", str(tmpl_dir)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "claude-doe-launcher.cmd.tmpl" in err


def test_render_writes_both_launchers(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--template-dir", str(tmpl_dir), "--no-smoke"])
    assert rc == 0
    home = Path(os.environ["CLAUDE_HOME"])
    cmd_dest = home / ".local" / "bin" / "claude-doe.cmd"
    ps1_dest = home / ".local" / "bin" / "claude-doe.ps1"
    assert cmd_dest.read_text() == (tmpl_dir / "claude-doe-launcher.cmd.tmpl").read_text()
    assert ps1_dest.read_text() == (tmpl_dir / "claude-doe-launcher.ps1.tmpl").read_text()
    err = capsys.readouterr().err
    assert f"Wrote launcher: {cmd_dest}" in err
    assert f"Wrote launcher: {ps1_dest}" in err
    assert "Done." in err


def test_render_is_idempotent_content(monkeypatch, tmp_path):
    _force_windows(monkeypatch)
    tmpl_dir = _make_template_dir(tmp_path)
    assert main(["--template-dir", str(tmpl_dir), "--no-smoke"]) == 0
    assert main(["--template-dir", str(tmpl_dir), "--no-smoke"]) == 0
    home = Path(os.environ["CLAUDE_HOME"])
    cmd_dest = home / ".local" / "bin" / "claude-doe.cmd"
    assert cmd_dest.read_text() == (tmpl_dir / "claude-doe-launcher.cmd.tmpl").read_text()


# ---------------------------------------------------------------------------
# --check-only dry-run safety
# ---------------------------------------------------------------------------


def test_check_only_does_not_write_live_files(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--check-only", "--template-dir", str(tmpl_dir)])
    assert rc == 0
    home = Path(os.environ["CLAUDE_HOME"])
    assert not (home / ".local" / "bin" / "claude-doe.cmd").exists()
    err = capsys.readouterr().err
    assert "would write" in err


def test_check_only_reports_up_to_date_after_real_render(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    tmpl_dir = _make_template_dir(tmp_path)
    assert main(["--template-dir", str(tmpl_dir), "--no-smoke"]) == 0
    capsys.readouterr()
    rc = main(["--check-only", "--template-dir", str(tmpl_dir)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "up to date" in err
    assert "would write" not in err


def test_check_only_survives_missing_tmpdir(monkeypatch, tmp_path, capsys):
    # Review: code-reviewer (2026-07-17 Finding 1) — closes the coverage gap the
    # reviewer named: the module's own target platform (clean Windows) never has
    # TMPDIR set, only TEMP/TMP (or nothing). This must not crash.
    _force_windows(monkeypatch)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--check-only", "--template-dir", str(tmpl_dir)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "would write" in err


# ---------------------------------------------------------------------------
# Post-render smoke check (F17)
#
# All tests here monkeypatch gen_claude_doe_launcher.smoke_launcher so nothing
# is ever really executed.
# ---------------------------------------------------------------------------


def test_smoke_skipped_when_no_smoke_flag(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    calls = []
    monkeypatch.setattr(
        gen_claude_doe_launcher, "smoke_launcher", lambda dest: calls.append(dest) or (0, "")
    )
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--template-dir", str(tmpl_dir), "--no-smoke"])
    assert rc == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "[smoke] skipped: --no-smoke was passed" in err


def test_smoke_skipped_on_non_windows_os_name(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    monkeypatch.setattr(gen_claude_doe_launcher.os, "name", "posix")
    calls = []
    monkeypatch.setattr(
        gen_claude_doe_launcher, "smoke_launcher", lambda dest: calls.append(dest) or (0, "")
    )
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--template-dir", str(tmpl_dir)])
    assert rc == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "[smoke] skipped:" in err
    assert "Windows" in err


def test_smoke_skipped_when_claude_cli_absent(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    monkeypatch.setattr(gen_claude_doe_launcher.os, "name", "nt")
    monkeypatch.setattr(gen_claude_doe_launcher.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(
        gen_claude_doe_launcher, "smoke_launcher", lambda dest: calls.append(dest) or (0, "")
    )
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--template-dir", str(tmpl_dir)])
    assert rc == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "[smoke] skipped:" in err
    assert "claude" in err


def test_smoke_runs_and_passes(monkeypatch, tmp_path, capsys):
    _force_windows(monkeypatch)
    monkeypatch.setattr(gen_claude_doe_launcher.os, "name", "nt")
    monkeypatch.setattr(
        gen_claude_doe_launcher.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else "/usr/bin/powershell",
    )
    monkeypatch.setattr(gen_claude_doe_launcher, "smoke_launcher", lambda dest: (0, ""))
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--template-dir", str(tmpl_dir)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[smoke] ok:" in err


def test_smoke_failure_with_empty_output_fails_install_regression(monkeypatch, tmp_path, capsys):
    """Regression net for the exact 2026-07-20 defect: claude-doe.cmd's
    cmd.exe paren-in-block parse failure produced a silent `exit /b 1`
    promoted to an unconditional `exit 1` with little/no output. A clean
    render must not be mistaken for a working launcher -- a non-zero smoke
    exit, even with EMPTY captured output, must fail the install phase."""
    _force_windows(monkeypatch)
    monkeypatch.setattr(gen_claude_doe_launcher.os, "name", "nt")
    monkeypatch.setattr(
        gen_claude_doe_launcher.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else "/usr/bin/powershell",
    )
    monkeypatch.setattr(gen_claude_doe_launcher, "smoke_launcher", lambda dest: (1, ""))
    tmpl_dir = _make_template_dir(tmp_path)
    rc = main(["--template-dir", str(tmpl_dir)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "[smoke] FAILED" in err
    assert "claude-doe-launcher.cmd.tmpl" in err
