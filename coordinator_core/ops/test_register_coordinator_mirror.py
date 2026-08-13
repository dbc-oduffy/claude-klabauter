"""Characterization + parity tests for coordinator_core.ops.register_coordinator_mirror.

Port of: register-coordinator-mirror.sh (example-doctrine-repo 6fb5fb37, 2026-07-22).
Spec backlink: docs/plans/2026-05-21-plugin-source-live-mirror-doctrine.md § Chunk 5 / AC-7
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.register_coordinator_mirror import (
    _SECTION_HEADER,
    main,
    register,
    resolve_registry_path,
)

_LIVE_PATH = "/Users/alice/X/example-doctrine-repo/coordinator"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets its own settings-home sandbox; no test touches the real registry."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home / ".coordinator-claude-settings"))
    return home


# ---------------------------------------------------------------------------
# resolve_registry_path — matches the bash oracle's `claude-home machine-local`
# ---------------------------------------------------------------------------


def test_resolve_registry_path_under_settings_home_machine_local(_isolated_env):
    reg_path = resolve_registry_path()
    assert reg_path == _isolated_env / ".coordinator-claude-settings" / "machine-local" / "registry.local.toml"


# ---------------------------------------------------------------------------
# register() — direct unit coverage of the write/idempotency/check-only logic
# ---------------------------------------------------------------------------


def test_check_only_absent_registry_reports_would_write(tmp_path, capsys):
    reg_path = tmp_path / "registry.local.toml"

    rc = register(reg_path, _LIVE_PATH, check_only=True)

    # Section header absent -- a real run would write it, so check-only now
    # fails loud rather than reporting an always-green 0.
    assert rc == 1
    assert "coordinator_plugin_mirrors: check failed:" in capsys.readouterr().out
    assert not reg_path.exists()


def test_write_creates_file_with_schema_and_section(tmp_path, capsys):
    reg_path = tmp_path / "registry.local.toml"

    rc = register(reg_path, _LIVE_PATH, check_only=False)

    assert rc == 0
    text = reg_path.read_text(encoding="utf-8")
    assert text.startswith("schema = 1\n")
    assert _SECTION_HEADER in text
    assert 'propagation_mode = "source_is_live"' in text
    assert f'live_path = "{_LIVE_PATH}"' in text
    assert "Coordinator plugin registered" in capsys.readouterr().out


def test_write_preserves_existing_content_and_appends(tmp_path):
    reg_path = tmp_path / "registry.local.toml"
    reg_path.write_text('schema = 1\nfoo = "bar"\n', encoding="utf-8")

    rc = register(reg_path, _LIVE_PATH, check_only=False)

    assert rc == 0
    text = reg_path.read_text(encoding="utf-8")
    assert text.startswith('schema = 1\nfoo = "bar"\n')
    assert _SECTION_HEADER in text


def test_write_is_idempotent_skips_when_already_registered(tmp_path, capsys):
    reg_path = tmp_path / "registry.local.toml"
    register(reg_path, _LIVE_PATH, check_only=False)
    capsys.readouterr()

    rc = register(reg_path, _LIVE_PATH, check_only=False)

    assert rc == 0
    assert "already registered — skipping." in capsys.readouterr().out


def test_check_only_after_write_reports_ready(tmp_path, capsys):
    reg_path = tmp_path / "registry.local.toml"
    register(reg_path, _LIVE_PATH, check_only=False)
    capsys.readouterr()

    rc = register(reg_path, _LIVE_PATH, check_only=True)

    assert rc == 0
    assert "coordinator_plugin_mirrors: ready" in capsys.readouterr().out


def test_write_escapes_toml_special_characters_in_live_path(tmp_path):
    reg_path = tmp_path / "registry.local.toml"
    tricky = 'C:\\Users\\o d"uffy'

    register(reg_path, tricky, check_only=False)

    text = reg_path.read_text(encoding="utf-8")
    assert 'live_path = "C:\\\\Users\\\\o d\\"uffy"' in text


def test_no_mtime_churn_on_idempotent_rerun(tmp_path):
    reg_path = tmp_path / "registry.local.toml"
    register(reg_path, _LIVE_PATH, check_only=False)
    first_text = reg_path.read_text(encoding="utf-8")

    register(reg_path, _LIVE_PATH, check_only=False)

    assert reg_path.read_text(encoding="utf-8") == first_text


# ---------------------------------------------------------------------------
# main() — CLI arg-parsing parity with the bash oracle
# ---------------------------------------------------------------------------


def test_main_requires_live_path(capsys):
    rc = main(["--check-only"])

    assert rc == 1
    assert "--live-path is required" in capsys.readouterr().err


def test_main_check_only_flag_form(_isolated_env, capsys):
    rc = main(["--check-only", "--live-path", _LIVE_PATH])

    assert rc == 1
    assert "would write" in capsys.readouterr().out


def test_main_live_path_equals_form(_isolated_env):
    # A real (non-check-only) run: this test asserts `--live-path=` equals-form
    # parsing, which is orthogonal to --check-only's own freshness exit code.
    rc = main(["--live-path=" + _LIVE_PATH])

    assert rc == 0


def test_main_writes_live_registry(_isolated_env):
    rc = main(["--live-path", _LIVE_PATH])

    assert rc == 0
    reg_path = resolve_registry_path()
    assert reg_path.exists()
    assert _LIVE_PATH in reg_path.read_text(encoding="utf-8")


def test_main_unrecognized_argv_token_silently_ignored(_isolated_env):
    """Matches the bash oracle's looseness: only argv[1] was ever inspected for the
    literal "--check-only" string; everything else was dropped on the floor."""
    rc = main(["--bogus-flag", "--live-path", _LIVE_PATH, "extra", "junk"])

    assert rc == 0
