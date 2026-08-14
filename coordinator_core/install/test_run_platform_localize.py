"""
Co-located pytest for coordinator_core.install.run_platform_localize
(install.md § Step 9 native port, DoE-claude repo). Covers: --check-only
never invokes platform_localize.main() nor mutates anything; a live run
that completes with rc 0 emits the "ran" status row and (when a validator
+ known_marketplaces.json are both present) runs the schema-validation
branch; a live run with an empty/absent plugins dir emits the maximalist
"no local plugin dirs" variant (install.md F9); and a non-zero
platform_localize.main() rc surfaces as the "error (see stderr)" row.

Spec backlink: coordinator/commands/install.md § Step 9 [DoE-claude repo]
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.install import run_platform_localize as rpl


def test_check_only_never_invokes_platform_localize(capsys, tmp_path):
    with mock.patch.object(rpl.platform_localize, "main") as mock_main:
        rc = rpl.run(
            check_only=True,
            python_bin="python3",
            known_marketplaces_path=tmp_path / "known_marketplaces.json",
            plugins_dir=tmp_path / "plugins",
        )

    mock_main.assert_not_called()
    assert rc == 0
    out = capsys.readouterr().out
    assert "platform_localize: skipped (check-only)" in out


def test_check_only_via_env_var(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CHECK_ONLY", "1")
    with mock.patch.object(rpl.platform_localize, "main") as mock_main:
        rc = rpl.main(
            [
                "--known-marketplaces-path", str(tmp_path / "known_marketplaces.json"),
                "--plugins-dir", str(tmp_path / "plugins"),
            ]
        )
    mock_main.assert_not_called()
    assert rc == 0
    assert "platform_localize: skipped (check-only)" in capsys.readouterr().out


def test_live_run_with_validation_present(capsys, tmp_path):
    known_mp = tmp_path / "known_marketplaces.json"
    known_mp.write_text("{}")
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "some-plugin").mkdir()

    validate_script = tmp_path / "validate-json-schemas.py"
    validate_script.write_text(
        "import sys\n"
        "print('known_marketplaces.json: passed')\n"
    )

    with mock.patch.object(rpl.platform_localize, "main", return_value=0) as mock_main:
        rc = rpl.run(
            check_only=False,
            python_bin="python3",
            known_marketplaces_path=known_mp,
            plugins_dir=plugins_dir,
            validate_schemas_path=validate_script,
        )

    mock_main.assert_called_once_with([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "known_marketplaces.json: passed" in out
    assert "platform_localize: ran" in out
    assert "no local plugin dirs" not in out


def test_live_run_no_local_plugin_dirs_is_not_a_failure(capsys, tmp_path):
    """install.md Step 9 "F9": under maximalist live-resolution, an absent
    or empty ~/.claude/plugins dir means platform-localize legitimately
    writes no known_marketplaces.json — this is an EXPECTED no-op, not an
    error, and must surface via the dedicated status-row variant."""
    known_mp = tmp_path / "known_marketplaces.json"  # never created
    plugins_dir = tmp_path / "plugins"  # never created — absent, not just empty

    with mock.patch.object(rpl.platform_localize, "main", return_value=0):
        rc = rpl.run(
            check_only=False,
            python_bin="python3",
            known_marketplaces_path=known_mp,
            plugins_dir=plugins_dir,
            validate_schemas_path=None,
        )

    assert rc == 0
    out = capsys.readouterr().out
    assert "known_marketplaces.json: not present" in out
    assert "platform_localize: ran (known_marketplaces.json not applicable — no local plugin dirs)" in out


def test_live_run_empty_plugins_dir_is_not_a_failure(capsys, tmp_path):
    known_mp = tmp_path / "known_marketplaces.json"
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()  # present but empty

    with mock.patch.object(rpl.platform_localize, "main", return_value=0):
        rc = rpl.run(
            check_only=False,
            python_bin="python3",
            known_marketplaces_path=known_mp,
            plugins_dir=plugins_dir,
            validate_schemas_path=None,
        )

    assert rc == 0
    out = capsys.readouterr().out
    assert "platform_localize: ran (known_marketplaces.json not applicable — no local plugin dirs)" in out


def test_platform_localize_error_surfaces_as_error_row(capsys, tmp_path):
    with mock.patch.object(rpl.platform_localize, "main", return_value=1):
        rc = rpl.run(
            check_only=False,
            python_bin="python3",
            known_marketplaces_path=tmp_path / "known_marketplaces.json",
            plugins_dir=tmp_path / "plugins",
        )

    assert rc == 1
    assert "platform_localize: error (see stderr)" in capsys.readouterr().out


def test_platform_localize_exception_surfaces_as_error_row(capsys, tmp_path):
    with mock.patch.object(rpl.platform_localize, "main", side_effect=RuntimeError("boom")):
        rc = rpl.run(
            check_only=False,
            python_bin="python3",
            known_marketplaces_path=tmp_path / "known_marketplaces.json",
            plugins_dir=tmp_path / "plugins",
        )

    assert rc == 1
    captured = capsys.readouterr()
    assert "platform_localize: error (see stderr)" in captured.out
    assert "boom" in captured.err


def test_cli_check_only_flag(capsys, tmp_path):
    with mock.patch.object(rpl.platform_localize, "main") as mock_main:
        rc = rpl.main(
            [
                "--check-only",
                "--known-marketplaces-path", str(tmp_path / "known_marketplaces.json"),
                "--plugins-dir", str(tmp_path / "plugins"),
            ]
        )
    mock_main.assert_not_called()
    assert rc == 0
    assert "platform_localize: skipped (check-only)" in capsys.readouterr().out
