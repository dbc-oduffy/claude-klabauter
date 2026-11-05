"""Tests for install_root_scope.redirected_install_reason."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.install.install_root_scope import redirected_install_reason

_VARS = ("COORDINATOR_SETTINGS_HOME", "CLAUDE_HOME", "CLAUDE_CONFIG_DIR")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)
    return h


def test_unset_is_none(home):
    assert redirected_install_reason() is None


def test_empty_is_none(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", "")
    assert redirected_install_reason() is None


@pytest.mark.parametrize("var", _VARS)
def test_each_var_redirected_names_it(home, tmp_path, monkeypatch, var):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv(var, str(sandbox))
    reason = redirected_install_reason()
    assert reason is not None and var in reason and str(sandbox) in reason
    assert "\n" not in reason


def test_override_equal_to_default_is_none(home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home / ".coordinator-claude-settings"))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    assert redirected_install_reason() is None


def test_trailing_sep_and_dotdot_equal_default(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude") + os.sep)
    monkeypatch.setenv("CLAUDE_HOME", str(home / ".claude" / ".."))
    assert redirected_install_reason() is None


def test_symlinked_home_is_not_a_redirect(home, tmp_path, monkeypatch):
    link = tmp_path / "homelink"
    try:
        os.symlink(home, link)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setenv("CLAUDE_HOME", str(link))
    assert redirected_install_reason() is None


def test_first_redirected_var_wins(home, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "x"))
    reason = redirected_install_reason()
    assert reason is not None and "CLAUDE_CONFIG_DIR" in reason


def _registered(monkeypatch, root):
    from types import SimpleNamespace

    from coordinator_core.install import engine_root_for_install

    monkeypatch.setattr(
        engine_root_for_install,
        "resolve_engine_root_for_install",
        lambda: SimpleNamespace(kind="published", root=root, remediation=None),
    )


def test_registered_clone_is_not_flagged(tmp_path, monkeypatch):
    from coordinator_core.install.install_root_scope import unregistered_clone_reason

    _registered(monkeypatch, tmp_path / "a")
    assert unregistered_clone_reason(tmp_path / "x" / ".." / "a") is None


def test_second_clone_names_both_roots(tmp_path, monkeypatch):
    from coordinator_core.install.install_root_scope import unregistered_clone_reason

    _registered(monkeypatch, tmp_path / "a")
    reason = unregistered_clone_reason(tmp_path / "b")
    assert reason is not None and str(tmp_path / "a") in reason and str(tmp_path / "b") in reason


def test_unresolvable_registry_is_flagged(tmp_path, monkeypatch):
    from coordinator_core.install.install_root_scope import unregistered_clone_reason

    _registered(monkeypatch, None)
    assert unregistered_clone_reason(tmp_path / "b") is not None
