"""test_check_mcp_versions_claude_config_dir.py — regression coverage for
check-mcp-versions.py's `_settings_path()`/`_marker_path()` routing through
coordinator_core._settings_home.claude_config_dir() instead of a hand-rolled
`os.path.expanduser("~") / ".claude"` join.

Spec backlink: cross-repo/inbox/2026-08-14-example-retrieval-repo-em-claude-home-routing-gap-c6-claude-klabauter-sites.md
"""
from __future__ import annotations

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

_SCRIPT_PATH = os.path.join(_BIN_DIR, "check-mcp-versions.py")
_spec = importlib.util.spec_from_file_location("check_mcp_versions_module", _SCRIPT_PATH)
_cmv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cmv)  # type: ignore[union-attr]


def test_settings_path_honours_claude_config_dir(monkeypatch, tmp_path):
    config_dir = tmp_path / "harness-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert _cmv._settings_path() == os.path.join(str(config_dir), "settings.json")


def test_marker_path_honours_claude_config_dir(monkeypatch, tmp_path):
    config_dir = tmp_path / "harness-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert _cmv._marker_path() == os.path.join(str(config_dir), ".mcp-version-check")


def test_settings_path_falls_back_to_home_dot_claude_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    expected = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    assert _cmv._settings_path() == expected
