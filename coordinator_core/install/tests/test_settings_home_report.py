"""Tests for coordinator_core.install.settings_home_report (C5, docs/plans/
2026-08-17-machine-first-install-surface.md).

Mutation-test contract asserted here: the check must fail when the settings
home stops being populated (a member removed on disk), never when only the
enumeration text changes -- see `test_missing_fixed_member_is_detected` and
`test_missing_forwarder_is_detected`, which each construct a fully-populated
fixture then remove exactly one real thing and assert the report flips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install.settings_home_report import (
    check_settings_home,
    format_report_lines,
)


def _populate_full_settings_home(root: Path) -> Path:
    sh = root / ".coordinator-claude-settings"
    (sh / "machine-local").mkdir(parents=True)
    (sh / "machine-local" / ".claude-klabauter-root").write_text("x")
    (sh / "bin").mkdir()
    (sh / "coordinator-whoami").mkdir()
    (sh / ".coordinator-venv").mkdir()
    (sh / "settings-manifest.md").write_text("x")
    (sh / ".percolate-identity").write_text("x")
    return sh


@pytest.fixture
def claude_klabauter_root() -> Path:
    """The real claude-klabauter checkout -- expected_forwarders() derives from its
    live coordinator/bin/ listing, so tests exercise the real generator
    rather than a synthetic fixture."""
    return Path(__file__).resolve().parents[3]


def test_fully_populated_fixed_members_report_present(tmp_path: Path, claude_klabauter_root: Path) -> None:
    sh = _populate_full_settings_home(tmp_path)

    report = check_settings_home(sh, claude_klabauter_root)

    assert report.fixed_missing == []


def test_missing_fixed_member_is_detected(tmp_path: Path, claude_klabauter_root: Path) -> None:
    sh = _populate_full_settings_home(tmp_path)
    (sh / "coordinator-whoami").rmdir()

    report = check_settings_home(sh, claude_klabauter_root)

    assert not report.complete
    labels = [m.label for m in report.fixed_missing]
    assert any("coordinator-whoami" in label for label in labels)


def test_missing_forwarder_is_detected(tmp_path: Path, claude_klabauter_root: Path) -> None:
    """An empty bin/ against a real coordinator/bin/ listing must report
    every expected forwarder missing, not silently pass."""
    sh = _populate_full_settings_home(tmp_path)

    report = check_settings_home(sh, claude_klabauter_root)

    assert report.forwarder_expected > 0
    assert report.forwarder_present == 0
    assert len(report.forwarder_missing) == report.forwarder_expected
    assert not report.complete


def test_forwarder_present_when_landed(tmp_path: Path, claude_klabauter_root: Path) -> None:
    """Landing exactly the expected forwarder files makes the check pass
    for forwarders specifically -- proves the oracle is the on-disk bin/
    listing, not a count or a self-reported manifest."""
    sh = _populate_full_settings_home(tmp_path)
    from coordinator_core.install.settings_home_report import expected_forwarders

    expected = expected_forwarders(claude_klabauter_root)
    for installed_name in expected:
        (sh / "bin" / installed_name).write_text("")

    report = check_settings_home(sh, claude_klabauter_root)

    assert report.forwarder_missing == []
    assert report.forwarder_present == report.forwarder_expected
    assert report.complete


def test_format_report_lines_flags_incomplete(tmp_path: Path, claude_klabauter_root: Path) -> None:
    sh = _populate_full_settings_home(tmp_path)
    (sh / "settings-manifest.md").unlink()

    report = check_settings_home(sh, claude_klabauter_root)
    lines = format_report_lines(report)

    assert any("FAIL" in line and "settings-manifest.md" in line for line in lines)


def test_check_does_not_leak_derivation_stdout(tmp_path: Path, claude_klabauter_root: Path, capsys) -> None:
    """coordinator_core.install.substrate._derive_agent_helper_target_map
    print()s a WARNING on a legacy extensionless/.py-twin collision; the
    doctor probe's caller emits pure JSON on stdout by contract, so that
    print must never reach this module's stdout. Regression pin for the
    stdout-pollution bug this module's docstring on `expected_forwarders`
    names."""
    sh = _populate_full_settings_home(tmp_path)

    check_settings_home(sh, claude_klabauter_root)

    assert capsys.readouterr().out == ""
