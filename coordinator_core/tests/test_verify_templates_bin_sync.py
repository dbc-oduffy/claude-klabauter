"""Characterization tests for coordinator_core.ops.verify_templates_bin_sync.

Post-reversal: live bin resolution primary is settings-home/bin (falling
back to the pre-migration ~/.claude/bin/), and --fix copies template -> live
(template canonical). See the module docstring and the cross-repo memo it
backlinks for the full rationale.

Port of: verify-templates-bin-sync.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.verify_templates_bin_sync import main


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated plugin_root + live-bin fixture, resolved via the SETTINGS-HOME
    primary path (not the CLAUDE_HOME fallback) — the common-case shape most
    tests exercise. Also isolates HOME/CLAUDE_HOME so a test cannot fall
    through to the developer's real home if settings-home resolution were
    ever to regress."""
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    settings_home_dir = tmp_path / "settings_home"
    live_bin = settings_home_dir / "bin"
    live_bin.mkdir(parents=True)
    fake_home = tmp_path / "unused_home"
    fake_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    return plugin_root, live_bin, plugin_root / "templates" / "bin"


def test_neither_present_all_not_present_exit_0(fixture, capsys):
    plugin_root, _live, _tmpl = fixture
    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("NOT_PRESENT") == 5
    assert "no files present" in out


def test_ok_when_byte_identical(fixture, capsys):
    plugin_root, live, tmpl = fixture
    (live / "claude_machine_local.py").write_text("hello\n")
    (tmpl / "claude_machine_local.py").write_text("hello\n")
    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert "OK           claude_machine_local.py" in out
    # 4 other pairs still NOT_PRESENT, exit stays 0
    assert rc == 0


def test_mismatch_flags_exit_1_in_verify_mode(fixture, capsys):
    plugin_root, live, tmpl = fixture
    (live / "resolve-coordinator-clone").write_text("diffbody\n")
    (tmpl / "resolve-coordinator-clone").write_text("different\n")
    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert "MISMATCH     resolve-coordinator-clone" in out
    assert rc == 1


def test_tmpl_missing_flags_exit_1_in_verify_mode(fixture, capsys):
    plugin_root, live, _tmpl = fixture
    (live / "claude-machine-local.sh").write_text("livecontent\n")
    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert "TMPL_MISSING claude-machine-local.sh" in out
    assert rc == 1


def test_tmpl_missing_is_unfixable_and_survives_fix_mode(fixture, capsys):
    plugin_root, live, tmpl = fixture
    (live / "claude-machine-local.sh").write_text("livecontent\n")
    rc = main([str(plugin_root), "--fix"])
    out = capsys.readouterr().out
    assert "TMPL_MISSING claude-machine-local.sh" in out
    # --fix cannot repair a TMPL_MISSING pair (nothing to copy FROM, the
    # template is canonical) — it still contributes to a nonzero exit code.
    assert rc == 1
    # No copy happened in either direction.
    assert not tmpl.joinpath("claude-machine-local.sh").exists()


def test_fix_mode_installs_live_missing_and_repairs_mismatch(fixture, capsys):
    plugin_root, live, tmpl = fixture
    (tmpl / "claude-machine-local.ps1").write_text("tmplonly\n")
    (live / "resolve-coordinator-clone").write_text("diffbody\n")
    (tmpl / "resolve-coordinator-clone").write_text("templatetruth\n")

    rc = main([str(plugin_root), "--fix"])
    out = capsys.readouterr().out
    assert "INSTALLED    claude-machine-local.ps1" in out
    assert "FIXED        resolve-coordinator-clone" in out
    # Live gets the template's content...
    assert (live / "claude-machine-local.ps1").read_text() == "tmplonly\n"
    assert (live / "resolve-coordinator-clone").read_text() == "templatetruth\n"
    # ...and the template is the source of truth: byte-unchanged. This is
    # the regression lock for the hazard the reversal exists to prevent —
    # a --fix run must never mutate example-doctrine-repo's template/source-repo content.
    assert (tmpl / "claude-machine-local.ps1").read_text() == "tmplonly\n"
    assert (tmpl / "resolve-coordinator-clone").read_text() == "templatetruth\n"
    # only NOT_PRESENT pairs remain -> exit 0
    assert rc == 0

    # Re-running verify mode afterward reports OK for the repaired pairs.
    rc2 = main([str(plugin_root)])
    out2 = capsys.readouterr().out
    assert "OK           claude-machine-local.ps1" in out2
    assert "OK           resolve-coordinator-clone" in out2
    assert rc2 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_fix_mode_preserves_destination_mode(fixture, capsys):
    """--fix copies content only — it must never apply or clear an exec bit
    on the live destination (matches _install_one: destination mode is
    preserved, not derived from the template's mode)."""
    plugin_root, live, tmpl = fixture
    live_path = live / "resolve-coordinator-clone"
    tmpl_path = tmpl / "resolve-coordinator-clone"
    live_path.write_text("diffbody\n")
    live_path.chmod(0o644)
    tmpl_path.write_text("templatetruth\n")
    tmpl_path.chmod(0o755)

    rc = main([str(plugin_root), "--fix"])
    capsys.readouterr()
    assert rc == 0
    assert (live_path.stat().st_mode & 0o777) == 0o644


def test_fix_mode_creates_live_bin_dir_for_live_missing(tmp_path, monkeypatch, capsys):
    """settings-home/bin absent entirely -> fallback selected -> the
    FALLBACK live bin dir (${CLAUDE_HOME}/.claude/bin) doesn't exist yet
    either -> --fix must mkdir -p it before installing."""
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    settings_home_dir = tmp_path / "settings_home"
    # Deliberately do NOT create settings_home_dir / "bin" -- forces fallback.
    fake_home = tmp_path / "claude_home_fallback"
    fake_home.mkdir()
    # Deliberately do NOT create fake_home / ".claude" / "bin" either.
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))

    tmpl_dir = plugin_root / "templates" / "bin"
    (tmpl_dir / "claude_machine_local.py").write_text("hi\n")

    rc = main([str(plugin_root), "--fix"])
    out = capsys.readouterr().out
    assert "INSTALLED    claude_machine_local.py" in out
    assert rc == 0
    live_bin = fake_home / ".claude" / "bin"
    assert (live_bin / "claude_machine_local.py").read_text() == "hi\n"


def test_bogus_mode_arg_behaves_as_verify(fixture, capsys):
    plugin_root, live, tmpl = fixture
    (live / "resolve-coordinator-clone").write_text("diffbody\n")
    (tmpl / "resolve-coordinator-clone").write_text("different\n")
    rc = main([str(plugin_root), "--bogus"])
    out = capsys.readouterr().out
    assert "MISMATCH     resolve-coordinator-clone" in out
    assert rc == 1


def test_missing_plugin_root_arg_returns_2(capsys):
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "missing required plugin_root argument" in err


def test_absent_templates_root_verify_mode_short_circuits(fixture, capsys):
    plugin_root, live, tmpl = fixture
    shutil.rmtree(tmpl)
    (live / "claude_machine_local.py").write_text("hello\n")
    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "TEMPLATES_ROOT_MISSING" in out
    assert str(tmpl) in out
    assert "TMPL_MISSING" not in out


def test_absent_templates_root_fix_mode_does_not_create_it(fixture, capsys):
    plugin_root, live, tmpl = fixture
    shutil.rmtree(tmpl)
    (live / "claude_machine_local.py").write_text("hello\n")
    rc = main([str(plugin_root), "--fix"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "TEMPLATES_ROOT_MISSING" in out
    assert not tmpl.exists()


def test_live_bin_resolves_to_settings_home_bin_when_present(fixture, capsys):
    plugin_root, live, tmpl = fixture
    (live / "claude_machine_local.py").write_text("hi\n")
    (tmpl / "claude_machine_local.py").write_text("hi\n")
    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert "OK           claude_machine_local.py" in out
    assert rc == 0


def test_live_bin_falls_back_to_claude_home_when_settings_home_bin_absent(
    tmp_path, monkeypatch, capsys
):
    """Fallback fires when settings-home/bin does not exist as a directory —
    isolate HOME/CLAUDE_HOME/COORDINATOR_SETTINGS_HOME so the test cannot
    read the developer's real home in either branch."""
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    settings_home_dir = tmp_path / "settings_home_without_bin"
    settings_home_dir.mkdir()
    home_substitute = tmp_path / "claude_home_fallback"
    fallback_live_bin = home_substitute / ".claude" / "bin"
    fallback_live_bin.mkdir(parents=True)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.setenv("CLAUDE_HOME", str(home_substitute))
    monkeypatch.setenv("HOME", str(home_substitute))

    (fallback_live_bin / "claude_machine_local.py").write_text("hi\n")
    (plugin_root / "templates" / "bin" / "claude_machine_local.py").write_text("hi\n")

    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert "OK           claude_machine_local.py" in out
    assert rc == 0


def test_claude_home_convention_a_appends_dot_claude(tmp_path, monkeypatch, capsys):
    """CLAUDE_HOME env var, when set, is a $HOME-substitute (Convention A) —
    /.claude/bin is appended, mirroring the retired bash oracle's
    `${CLAUDE_HOME:-$HOME}/.claude` exactly (not treated as a full
    .claude-substitute path). This convention now governs only the
    migration-window fallback (settings-home/bin absent)."""
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    settings_home_dir = tmp_path / "settings_home_without_bin"
    settings_home_dir.mkdir()
    home_substitute = tmp_path / "some_home"
    live_bin = home_substitute / ".claude" / "bin"
    live_bin.mkdir(parents=True)
    (live_bin / "claude_machine_local.py").write_text("hi\n")
    (plugin_root / "templates" / "bin" / "claude_machine_local.py").write_text("hi\n")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.setenv("CLAUDE_HOME", str(home_substitute))
    rc = main([str(plugin_root)])
    out = capsys.readouterr().out
    assert "OK           claude_machine_local.py" in out
    assert rc == 0
