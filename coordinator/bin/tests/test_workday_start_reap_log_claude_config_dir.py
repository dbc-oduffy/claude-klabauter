"""test_workday_start_reap_log_claude_config_dir.py — regression coverage for
workday-start-day-branch-resolve.py's `cmd_reap_log` routing the
coordinator-reap.log append through
coordinator_core._settings_home.claude_config_dir() instead of a hand-rolled
`Path.home() / ".claude"`.

Spec backlink: cross-repo/inbox/2026-08-14-example-retrieval-repo-em-claude-home-routing-gap-c6-claude-klabauter-sites.md
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import pytest

pytestmark = pytest.mark.cadence

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_BIN_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SCRIPT_PATH = os.path.join(_BIN_DIR, "workday-start-day-branch-resolve.py")
_spec = importlib.util.spec_from_file_location("workday_start_day_branch_resolve_module", _SCRIPT_PATH)
_wsdbr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wsdbr)  # type: ignore[union-attr]


def test_reap_log_writes_under_claude_config_dir(monkeypatch, tmp_path):
    config_dir = tmp_path / "harness-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_wsdbr, "_run_reap_sessions", lambda: "reaped 2 sessions")

    rc = _wsdbr.cmd_reap_log(argparse.Namespace())

    assert rc == 0
    log_file = config_dir / "logs" / "coordinator-reap.log"
    assert log_file.is_file()
    assert "reaped 2 sessions" in log_file.read_text(encoding="utf-8")


def test_reap_log_no_write_when_reap_output_empty(monkeypatch, tmp_path):
    config_dir = tmp_path / "harness-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_wsdbr, "_run_reap_sessions", lambda: "")

    rc = _wsdbr.cmd_reap_log(argparse.Namespace())

    assert rc == 0
    assert not (config_dir / "logs" / "coordinator-reap.log").exists()
