"""Characterization tests for coordinator_core.ops.verify_templates_setup_sync.

Built against the golden-oracle behavior of the coordinator-claude bash script, run
against a scratch CLAUDE_HOME/plugin-root pair — OK / MISMATCH /
LIVE_MISSING / TMPL_MISSING / NOT_PRESENT-for-all-pairs, plus the
exit-code arbitration rule.

Port of: verify-templates-setup-sync.sh (coordinator-claude b5a4192c, 2026-07-20)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.verify_templates_setup_sync import (
    PAIRS,
    PluginRootUnresolved,
    _resolve_plugin_root,
    check_pairs,
    main,
)


def _make_pair_dirs(tmp_path: Path) -> tuple[Path, Path]:
    templates = tmp_path / "templates" / "setup"
    live = tmp_path / "home" / ".claude" / "setup"
    templates.mkdir(parents=True)
    live.mkdir(parents=True)
    return templates, live


def test_all_ok_when_every_pair_byte_identical(tmp_path):
    templates, live = _make_pair_dirs(tmp_path)
    for live_name, tmpl_name in PAIRS:
        (templates / tmpl_name).parent.mkdir(parents=True, exist_ok=True)
        (live / live_name).parent.mkdir(parents=True, exist_ok=True)
        (templates / tmpl_name).write_text("same content\n")
        (live / live_name).write_text("same content\n")

    lines, rc = check_pairs(templates, live)
    assert rc == 0
    assert all(line.startswith("OK") for line in lines)
    assert len(lines) == len(PAIRS)


def test_mismatch_sets_nonzero_exit(tmp_path):
    templates, live = _make_pair_dirs(tmp_path)
    live_name, tmpl_name = PAIRS[0]
    (templates / tmpl_name).parent.mkdir(parents=True, exist_ok=True)
    (live / live_name).parent.mkdir(parents=True, exist_ok=True)
    (templates / tmpl_name).write_text("template version\n")
    (live / live_name).write_text("drifted live version\n")

    lines, rc = check_pairs(templates, live)
    assert rc == 1
    assert f"MISMATCH     {live_name}" in lines


def test_live_missing_reported_and_nonzero_exit(tmp_path):
    templates, live = _make_pair_dirs(tmp_path)
    live_name, tmpl_name = PAIRS[0]
    (templates / tmpl_name).parent.mkdir(parents=True, exist_ok=True)
    (templates / tmpl_name).write_text("template only\n")

    lines, rc = check_pairs(templates, live)
    assert rc == 1
    assert any(line.startswith(f"LIVE_MISSING {live_name}") for line in lines)


def test_tmpl_missing_reported_and_nonzero_exit(tmp_path):
    templates, live = _make_pair_dirs(tmp_path)
    live_name, tmpl_name = PAIRS[0]
    (live / live_name).parent.mkdir(parents=True, exist_ok=True)
    (live / live_name).write_text("live only\n")

    lines, rc = check_pairs(templates, live)
    assert rc == 1
    assert any(line.startswith(f"TMPL_MISSING {live_name}") for line in lines)


def test_neither_side_present_for_any_pair_is_graceful_zero_exit(tmp_path):
    templates, live = _make_pair_dirs(tmp_path)

    lines, rc = check_pairs(templates, live)
    assert rc == 0
    assert all(
        line.startswith("NOT_PRESENT") or line.startswith("no files present")
        for line in lines
    )
    assert lines[-1].startswith("no files present")


def test_negative_stray_arg_warns_but_still_runs(tmp_path, monkeypatch, capsys):
    templates, live = _make_pair_dirs(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))

    rc = main(["--fix"])
    captured = capsys.readouterr()
    assert "WARNING: verify-templates-setup-sync takes no flags" in captured.err
    assert rc == 0
    assert "NOT_PRESENT" in captured.out


def test_negative_no_args_no_warning(tmp_path, monkeypatch, capsys):
    templates, live = _make_pair_dirs(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))

    rc = main([])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert rc == 0


def test_unset_plugin_root_raises_instead_of_falling_back_to_cwd(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with pytest.raises(PluginRootUnresolved):
        _resolve_plugin_root()


def test_unset_plugin_root_makes_main_fail_loud_not_cwd_fallback(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", "/nonexistent/claude/home")

    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "CLAUDE_PLUGIN_ROOT is unset" in captured.err
