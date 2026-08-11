"""Characterization + parity tests for coordinator_core.ops.gen_claude_doe_shim.

Spec backlink: docs/plans/2026-07-04-coordinator-maximalist-install-shape.md § C2
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.gen_claude_doe_shim import (
    EXPECTED_SOURCE_LINE,
    EXPECTED_SOURCE_LINE_POWERSHELL,
    SENTINEL_BEGIN,
    SENTINEL_END,
    main,
)


def _make_template(tmp_path: Path, lines: int = 3) -> Path:
    tmpl = tmp_path / "claude-doe-shim.sh.tmpl"
    tmpl.write_text("\n".join(f"# template line {i}" for i in range(lines)) + "\n")
    return tmpl


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets its own HOME/CLAUDE_HOME sandbox; no test touches a real rc."""
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.delenv("COORDINATOR_SHIM_RC", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return home


# ---------------------------------------------------------------------------
# CLI arg parsing
# ---------------------------------------------------------------------------


def test_help_exits_zero(tmp_path, capsys):
    rc = main(["--help", "--template", str(_make_template(tmp_path))])
    assert rc == 0
    assert "Usage: gen-claude-doe-shim.sh" in capsys.readouterr().err


def test_unknown_argument_is_silently_ignored(tmp_path, monkeypatch, capsys):
    """2026-07-23 M3/D9 collapse: unrecognized argv tokens (e.g. a caller
    forwarding a whole ``${ARGUMENTS}`` blob containing ``--non-interactive``/
    ``--reconfigure``) must not fail this generator. Supersedes the prior
    strict fail-loud "Unknown argument" contract."""
    monkeypatch.setenv("HOME", str(tmp_path / "home2"))
    (tmp_path / "home2").mkdir()
    # Use a real (non-check-only) run: --check-only's own exit code now
    # reflects shim/rc freshness (see § check-only dry-run safety below),
    # which is orthogonal to what this test asserts -- unknown argv tokens
    # must not themselves trip a hard failure.
    rc = main(["--bogus", "--non-interactive", "--template", str(_make_template(tmp_path))])
    assert rc == 0


def test_missing_rc_value_exits_one(tmp_path, capsys):
    rc = main(["--template", str(_make_template(tmp_path)), "--rc"])
    assert rc == 1
    assert "--rc requires a value" in capsys.readouterr().err


def test_missing_template_value_exits_one(capsys):
    rc = main(["--template"])
    assert rc == 1
    assert "--template requires a value" in capsys.readouterr().err


def test_template_not_supplied_is_fail_loud(capsys):
    rc = main([])
    assert rc == 1
    assert "--template not supplied" in capsys.readouterr().err


def test_template_not_found_exits_one(tmp_path, capsys):
    rc = main(["--template", str(tmp_path / "nope.tmpl")])
    assert rc == 1
    assert "Template not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# render + rc wiring (positive path)
# ---------------------------------------------------------------------------


def test_fresh_render_writes_shim_and_wires_rc(tmp_path, monkeypatch, capsys):
    home = Path(os.environ["HOME"])
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    shim_dest = home / ".claude" / "shell" / "claude-doe-shim.sh"
    assert shim_dest.read_text() == tmpl.read_text()
    rc_file = home / ".zshrc"
    assert rc_file.exists()
    text = rc_file.read_text()
    assert SENTINEL_BEGIN in text
    assert SENTINEL_END in text
    assert EXPECTED_SOURCE_LINE in text
    err = capsys.readouterr().err
    assert "Wrote shim:" in err
    assert "source block added" in err
    assert "Done." in err


def test_rerun_is_idempotent_noop(tmp_path):
    home = Path(os.environ["HOME"])
    tmpl = _make_template(tmp_path)
    assert main(["--template", str(tmpl)]) == 0
    rc_file = home / ".zshrc"
    before = rc_file.read_text()
    assert main(["--template", str(tmpl)]) == 0
    after = rc_file.read_text()
    assert before == after


def test_rerun_reports_noop_message(tmp_path, capsys):
    tmpl = _make_template(tmp_path)
    assert main(["--template", str(tmpl)]) == 0
    capsys.readouterr()
    assert main(["--template", str(tmpl)]) == 0
    assert "sentinel block unchanged (no-op)" in capsys.readouterr().err


def test_hand_modified_sentinel_body_fails_loud(tmp_path, monkeypatch, capsys):
    home = Path(os.environ["HOME"])
    rc_file = home / ".zshrc"
    rc_file.write_text(
        f"some prior content\n\n{SENTINEL_BEGIN}\n"
        f'echo "hand modified"\n{SENTINEL_END}\n'
    )
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "hand-modified" in err
    assert 'echo "hand modified"' in err
    # Rc file must be left untouched — fail-loud does not silently clobber.
    assert rc_file.read_text() == (
        f"some prior content\n\n{SENTINEL_BEGIN}\n"
        f'echo "hand modified"\n{SENTINEL_END}\n'
    )


# ---------------------------------------------------------------------------
# --rc / COORDINATOR_SHIM_RC / $SHELL / MSYSTEM precedence
# ---------------------------------------------------------------------------


def test_rc_flag_overrides_shell_detection(tmp_path):
    custom_rc = tmp_path / "custom_rc"
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl), "--rc", str(custom_rc)])
    assert rc == 0
    assert custom_rc.exists()
    assert SENTINEL_BEGIN in custom_rc.read_text()


def test_coordinator_shim_rc_env_used_when_no_flag(tmp_path, monkeypatch):
    env_rc = tmp_path / "env_rc"
    monkeypatch.setenv("COORDINATOR_SHIM_RC", str(env_rc))
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    assert env_rc.exists()


def test_rc_flag_takes_precedence_over_env(tmp_path, monkeypatch):
    env_rc = tmp_path / "env_rc"
    flag_rc = tmp_path / "flag_rc"
    monkeypatch.setenv("COORDINATOR_SHIM_RC", str(env_rc))
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl), "--rc", str(flag_rc)])
    assert rc == 0
    assert flag_rc.exists()
    assert not env_rc.exists()


def test_bash_shell_detection_targets_bashrc(tmp_path, monkeypatch):
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("SHELL", "/bin/bash")
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    assert (home / ".bashrc").exists()
    assert not (home / ".zshrc").exists()


def test_msystem_env_targets_bashrc_regardless_of_shell(tmp_path, monkeypatch):
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    assert (home / ".bashrc").exists()
    assert not (home / ".zshrc").exists()


def test_unrecognised_shell_warns_and_defaults_to_zshrc(tmp_path, monkeypatch, capsys):
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    assert (home / ".zshrc").exists()
    assert "not recognised; defaulting to ~/.zshrc" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# legacy-stopgap detection
# ---------------------------------------------------------------------------


def test_legacy_stopgap_detected_in_home_bashrc(tmp_path, monkeypatch, capsys):
    home = Path(os.environ["HOME"])
    (home / ".bashrc").write_text(
        "# --- coordinator maximalist launch ---\nold stuff\n# end\n"
    )
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOTE (migration): legacy coordinator maximalist launch" in err


def test_legacy_stopgap_detected_with_padded_line_form(tmp_path, capsys):
    """The real-world hand-written block carries a trailing comment/padding
    suffix on the marker line (e.g. ``... (example-doctrine-repo-resident plugin source) ---...``)
    rather than the bare marker in isolation. The detector must match on the
    marker as a line prefix (after strip), not whole-line equality, or it
    never fires on the one machine that actually has the legacy block."""
    home = Path(os.environ["HOME"])
    (home / ".bashrc").write_text(
        "# --- coordinator maximalist launch (example-doctrine-repo-resident plugin source) "
        "----------------\nold stuff\n# end\n"
    )
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOTE (migration): legacy coordinator maximalist launch" in err


def test_legacy_stopgap_absent_no_note(tmp_path, capsys):
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOTE (migration)" not in err


# ---------------------------------------------------------------------------
# --check-only dry-run safety
# ---------------------------------------------------------------------------


def test_check_only_does_not_write_live_files(tmp_path, capsys):
    home = Path(os.environ["HOME"])
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl), "--check-only"])
    # shim_dest is absent -- a real run would write it, so --check-only now
    # fails loud rather than reporting an always-green 0.
    assert rc == 1
    assert not (home / ".claude" / "shell" / "claude-doe-shim.sh").exists()
    assert not (home / ".zshrc").exists()
    err = capsys.readouterr().err
    assert "sentinel absent" in err
    assert "would write to" in err


def test_check_only_reports_noop_after_real_render(tmp_path, capsys):
    tmpl = _make_template(tmp_path)
    assert main(["--template", str(tmpl)]) == 0
    capsys.readouterr()
    rc = main(["--template", str(tmpl), "--check-only"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "sentinel block present and unmodified (would be no-op)" in err


def test_check_only_reports_hand_modified_fail_loud(tmp_path, capsys):
    home = Path(os.environ["HOME"])
    rc_file = home / ".zshrc"
    rc_file.write_text(f"{SENTINEL_BEGIN}\nsomething else\n{SENTINEL_END}\n")
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl), "--check-only"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "HAND-MODIFIED" in err
    # --check-only never mutates the live rc.
    assert rc_file.read_text() == f"{SENTINEL_BEGIN}\nsomething else\n{SENTINEL_END}\n"


def test_check_only_survives_missing_tmpdir(tmp_path, monkeypatch, capsys):
    # Mirrors the Windows-portability fix in the sibling gen_claude_doe_launcher.py
    # port: this module's own target platform (clean Windows) never has TMPDIR
    # set, only TEMP/TMP (or nothing). tempfile.gettempdir() must not crash, unlike
    # the bash oracle's hardcoded `mktemp /tmp/...` which assumes /tmp exists.
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl), "--check-only"])
    assert rc == 1
    assert "would write" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLAUDE_HOME vs HOME split (legacy-detector faithfully reads $HOME, not
# CLAUDE_HOME -- negative-spec preserved verbatim from the bash oracle)
# ---------------------------------------------------------------------------


def test_claude_home_override_used_for_shim_dest_not_legacy_detector(
    tmp_path, monkeypatch, capsys
):
    real_home = Path(os.environ["HOME"])
    claude_home = tmp_path / "claude_home_base"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    # Legacy marker lives under the REAL $HOME, not CLAUDE_HOME.
    (real_home / ".bashrc").write_text(
        "# --- coordinator maximalist launch ---\nold\n# end\n"
    )
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    # Shim lands under CLAUDE_HOME.
    assert (claude_home / ".claude" / "shell" / "claude-doe-shim.sh").exists()
    # Legacy note still fires because it reads $HOME/.bashrc, not CLAUDE_HOME.
    assert "NOTE (migration)" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --graceful-skip-unresolved + contract status rows (2026-07-23 M3/D9)
# ---------------------------------------------------------------------------


def test_graceful_skip_unresolved_exits_zero_with_skip_row(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)
    monkeypatch.setenv("PATH", "")  # no machine-local resolvable
    rc = main(["--graceful-skip-unresolved", "--template", str(_make_template(tmp_path))])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude_shim: skipped (example-doctrine-repo clone not resolved" in out


def test_graceful_skip_unresolved_noop_when_resolved(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(tmp_path))
    rc = main(["--graceful-skip-unresolved", "--template", str(_make_template(tmp_path))])
    assert rc == 0
    assert "claude_shim: installed (" in capsys.readouterr().out


def test_installed_row_on_fresh_render(tmp_path, capsys):
    rc = main(["--template", str(_make_template(tmp_path))])
    assert rc == 0
    assert "claude_shim: installed (" in capsys.readouterr().out


def test_ready_noop_row_on_second_run(tmp_path, capsys):
    tmpl = str(_make_template(tmp_path))
    assert main(["--template", tmpl]) == 0
    capsys.readouterr()
    rc = main(["--template", tmpl])
    assert rc == 0
    assert "claude_shim: ready (no-op)" in capsys.readouterr().out


def test_would_install_row_under_check_only(tmp_path, capsys):
    rc = main(["--check-only", "--template", str(_make_template(tmp_path))])
    assert rc == 1
    out = capsys.readouterr().out
    assert "claude_shim: check failed:" in out
    assert "would install" in out


def test_check_only_ready_noop_row_after_real_render(tmp_path, capsys):
    tmpl = str(_make_template(tmp_path))
    assert main(["--template", tmpl]) == 0
    capsys.readouterr()

    rc = main(["--template", tmpl, "--check-only"])

    assert rc == 0
    assert "claude_shim: ready (no-op) (" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --shell powershell family
# ---------------------------------------------------------------------------


def test_default_no_flag_still_produces_bash_line_and_path(tmp_path):
    """Regression that matters most: omitting --shell entirely must be
    byte-identical to today's behaviour (bash source line, .sh shim path)."""
    home = Path(os.environ["HOME"])
    tmpl = _make_template(tmp_path)
    rc = main(["--template", str(tmpl)])
    assert rc == 0
    shim_dest = home / ".claude" / "shell" / "claude-doe-shim.sh"
    assert shim_dest.exists()
    rc_text = (home / ".zshrc").read_text()
    assert EXPECTED_SOURCE_LINE in rc_text
    assert "Test-Path" not in rc_text


def test_shell_powershell_writes_dot_source_line_and_ps1_shim(tmp_path):
    home = Path(os.environ["HOME"])
    tmpl = _make_template(tmp_path)
    custom_rc = tmp_path / "profile.ps1"
    rc = main(["--template", str(tmpl), "--shell", "powershell", "--rc", str(custom_rc)])
    assert rc == 0
    shim_dest = home / ".claude" / "shell" / "claude-doe-shim.ps1"
    assert shim_dest.exists()
    assert not (home / ".claude" / "shell" / "claude-doe-shim.sh").exists()
    rc_text = custom_rc.read_text()
    assert EXPECTED_SOURCE_LINE_POWERSHELL in rc_text
    assert ". $__claude_doe_shim_path" in rc_text


def test_shell_powershell_rerun_is_idempotent_noop(tmp_path):
    tmpl = _make_template(tmp_path)
    custom_rc = tmp_path / "profile.ps1"
    assert main(["--template", str(tmpl), "--shell", "powershell", "--rc", str(custom_rc)]) == 0
    before = custom_rc.read_text()
    assert main(["--template", str(tmpl), "--shell", "powershell", "--rc", str(custom_rc)]) == 0
    after = custom_rc.read_text()
    assert before == after


def test_shell_powershell_hand_modified_fails_loud(tmp_path, capsys):
    tmpl = _make_template(tmp_path)
    custom_rc = tmp_path / "profile.ps1"
    custom_rc.write_text(f"{SENTINEL_BEGIN}\nWrite-Host 'hand modified'\n{SENTINEL_END}\n")
    rc = main(["--template", str(tmpl), "--shell", "powershell", "--rc", str(custom_rc)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "hand-modified" in err
    assert "Write-Host 'hand modified'" in err
    assert custom_rc.read_text() == (
        f"{SENTINEL_BEGIN}\nWrite-Host 'hand modified'\n{SENTINEL_END}\n"
    )


def test_shell_unknown_value_exits_one(tmp_path, capsys):
    rc = main(["--template", str(_make_template(tmp_path)), "--shell", "fish"])
    assert rc == 1
    assert "--shell must be one of" in capsys.readouterr().err


def test_shell_missing_value_exits_one(tmp_path, capsys):
    rc = main(["--template", str(_make_template(tmp_path)), "--shell"])
    assert rc == 1
    assert "--shell requires a value" in capsys.readouterr().err
