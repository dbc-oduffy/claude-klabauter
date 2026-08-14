"""
Tests for coordinator_core.ops.probe_onboarding_currency.

Port of: test-probe-onboarding-currency.sh (DoE 894d4bc6, 2026-07-22) — the bash
suite's 8 cases are the parity oracle for this port, mirrored case for case.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.probe_onboarding_currency import main


def _run(monkeypatch, repo_root, plugin_root, extra_env=None):
    for key in (
        "CLAUDE_HOME",
        "COORDINATOR_CURRENCY_REPO_ROOT",
        "COORDINATOR_CURRENCY_PLUGIN_ROOT",
        "COORDINATOR_CURRENCY_SOURCE_IS_LIVE",
        "COORDINATOR_CURRENCY_SCRIPT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COORDINATOR_CURRENCY_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("COORDINATOR_CURRENCY_PLUGIN_ROOT", str(plugin_root))
    for k, v in (extra_env or {}).items():
        monkeypatch.setenv(k, v)

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([])
    return buf.getvalue().strip(), rc


def _make_sandbox(tmp_path, name, schema_version):
    repo_root = tmp_path / name / "fake-claude-home"
    plugin_root = tmp_path / name / "fake-plugin-root"
    (repo_root / "docs").mkdir(parents=True)
    plugin_root.mkdir(parents=True)
    (plugin_root / "coordinator-schema-version").write_text(f"{schema_version}\n")
    return repo_root, plugin_root


def test_case1_stamped_current(tmp_path, monkeypatch):
    repo_root, plugin_root = _make_sandbox(tmp_path, "case1", "1")
    (repo_root / "docs" / "coordinator-currency.yaml").write_text(
        "schema_version: 1\nstamped_at: 2026-05-29\n"
    )
    out, rc = _run(monkeypatch, repo_root, plugin_root)
    assert out == "current"
    assert rc == 0


def test_case2_drift(tmp_path, monkeypatch):
    repo_root, plugin_root = _make_sandbox(tmp_path, "case2", "3")
    (repo_root / "docs" / "coordinator-currency.yaml").write_text(
        "schema_version: 1\nstamped_at: 2026-01-01\n"
    )
    out, rc = _run(monkeypatch, repo_root, plugin_root)
    assert out == "drift(1->3)"
    assert rc == 0


def test_case3_unstamped_legacy(tmp_path, monkeypatch):
    repo_root, plugin_root = _make_sandbox(tmp_path, "case3", "1")
    out, rc = _run(monkeypatch, repo_root, plugin_root)
    assert out == "unstamped(legacy)"
    assert rc == 0
    assert out != "current"


def test_case4_inconclusive_schema_unreadable(tmp_path, monkeypatch):
    repo_root = tmp_path / "case4" / "fake-claude-home"
    (repo_root / "docs").mkdir(parents=True)
    empty_plugin_root = tmp_path / "case4" / "empty-plugin-root"
    empty_plugin_root.mkdir(parents=True)
    out, rc = _run(monkeypatch, repo_root, empty_plugin_root)
    assert out.startswith("inconclusive(")
    assert "schema constant unreadable" in out
    assert rc == 0


def test_case5_inconclusive_stamp_unparseable(tmp_path, monkeypatch):
    repo_root, plugin_root = _make_sandbox(tmp_path, "case5", "1")
    (repo_root / "docs" / "coordinator-currency.yaml").write_text(
        "stamped_at: 2026-05-01\n"
    )
    out, rc = _run(monkeypatch, repo_root, plugin_root)
    assert out.startswith("inconclusive(")
    assert "unparseable" in out
    assert rc == 0


def test_case6_source_is_live(tmp_path, monkeypatch):
    repo_root = tmp_path / "case6" / "chome"
    (repo_root / "docs").mkdir(parents=True)
    plugin_root = tmp_path / "case6" / "fake-plugin-root"
    plugin_root.mkdir(parents=True)
    (plugin_root / "coordinator-schema-version").write_text("1\n")
    out, rc = _run(
        monkeypatch, repo_root, plugin_root, {"COORDINATOR_CURRENCY_SOURCE_IS_LIVE": "1"}
    )
    assert out == "source_is_live"
    assert rc == 0


def test_case7_forward_stamp_inconclusive(tmp_path, monkeypatch):
    repo_root, plugin_root = _make_sandbox(tmp_path, "case7", "1")
    (repo_root / "docs" / "coordinator-currency.yaml").write_text(
        "schema_version: 5\nstamped_at: 2026-05-29\n"
    )
    out, rc = _run(monkeypatch, repo_root, plugin_root)
    assert out.startswith("inconclusive(")
    assert rc == 0


def test_case8_default_claude_home_fatal_guard(tmp_path, monkeypatch):
    """F14 guard: CLAUDE_HOME already ending in /.claude → FATAL, exit 1."""
    bad_home = tmp_path / "homeF" / ".claude"
    bad_home.mkdir(parents=True)
    for key in (
        "CLAUDE_HOME",
        "COORDINATOR_CURRENCY_REPO_ROOT",
        "COORDINATOR_CURRENCY_PLUGIN_ROOT",
        "COORDINATOR_CURRENCY_SOURCE_IS_LIVE",
        "COORDINATOR_CURRENCY_SCRIPT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(bad_home))
    monkeypatch.setenv("COORDINATOR_CURRENCY_PLUGIN_ROOT", str(tmp_path))
    rc = main([])
    assert rc == 1


def test_case9_default_claude_home_ok(tmp_path, monkeypatch):
    """CLAUDE_HOME not ending in /.claude → appended, no stamp → unstamped(legacy)."""
    home = tmp_path / "homeF"
    (home / ".claude" / "docs").mkdir(parents=True)
    plugin_root = tmp_path / "plugin1"
    plugin_root.mkdir(parents=True)
    (plugin_root / "coordinator-schema-version").write_text("1\n")
    for key in (
        "CLAUDE_HOME",
        "COORDINATOR_CURRENCY_REPO_ROOT",
        "COORDINATOR_CURRENCY_PLUGIN_ROOT",
        "COORDINATOR_CURRENCY_SOURCE_IS_LIVE",
        "COORDINATOR_CURRENCY_SCRIPT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_CURRENCY_PLUGIN_ROOT", str(plugin_root))

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([])
    assert buf.getvalue().strip() == "unstamped(legacy)"
    assert rc == 0


def test_case10_default_repo_root_uses_userprofile_when_home_absent(tmp_path, monkeypatch):
    """Native-Windows condition for the default `repo_root` derivation
    (home-resolution-lint bare_home_or_chain fix, 2026-07-29): CLAUDE_HOME
    and HOME both absent, only USERPROFILE set. Must resolve `repo_root` via
    USERPROFILE rather than degrading to a bare `/.claude` (cwd-relative)."""
    home = tmp_path / "homeF"
    (home / ".claude" / "docs").mkdir(parents=True)
    plugin_root = tmp_path / "plugin1"
    plugin_root.mkdir(parents=True)
    (plugin_root / "coordinator-schema-version").write_text("1\n")
    for key in (
        "CLAUDE_HOME",
        "HOME",
        "COORDINATOR_CURRENCY_REPO_ROOT",
        "COORDINATOR_CURRENCY_PLUGIN_ROOT",
        "COORDINATOR_CURRENCY_SOURCE_IS_LIVE",
        "COORDINATOR_CURRENCY_SCRIPT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("COORDINATOR_CURRENCY_PLUGIN_ROOT", str(plugin_root))

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([])
    assert buf.getvalue().strip() == "unstamped(legacy)"
    assert rc == 0
