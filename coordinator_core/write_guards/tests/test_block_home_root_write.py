"""Behavioral tests for the home-root Write/Edit guard, driven through ``check()`` and the engine."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.write_guards import block_home_root_write as guard
from coordinator_core.write_guards import engine
from coordinator_core.write_guards._home_root import DENY_REASON


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: h))
    return h


def _payload(path, tool="Write", key="file_path", cwd=None):
    payload = {"tool_name": tool, "tool_input": {key: str(path)}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def test_write_of_new_home_file_is_denied(home):
    out = guard.check(_payload(home / "fo.txt"))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == DENY_REASON


def test_notebook_path_is_judged(home):
    assert guard.check(_payload(home / "n.ipynb", "NotebookEdit", "notebook_path")) is not None


def test_tool_owned_dotfile_is_allowed(home):
    assert guard.check(_payload(home / ".gitconfig")) is None


def test_edit_of_existing_home_file_is_allowed(home):
    existing = home / "keep.txt"
    existing.write_text("x", encoding="utf-8")
    assert guard.check(_payload(existing, "Edit")) is None


def test_home_subdir_is_allowed(home):
    assert guard.check(_payload(home / "sub" / "fo.txt")) is None


def test_malformed_payload_allows(home):
    assert guard.check({"tool_input": "nope"}) is None
    assert guard.check({}) is None


@pytest.mark.parametrize("level,decision", [("warn", "allow"), ("off", None)])
def test_guard_level_downgrades_the_deny(home, monkeypatch, level, decision):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
    machine_profile.reset_cache()
    out = guard.check(_payload(home / "fo.txt"))
    if decision is None:
        assert out is None
    else:
        assert out["hookSpecificOutput"]["permissionDecision"] == decision


def test_home_under_a_git_repo_this_guard_wins_over_out_of_repo_bump(home, tmp_path):
    other = tmp_path / "other-repo"
    (other / ".git").mkdir(parents=True)
    result = engine.evaluate(_payload(home / "fo.txt", cwd=other), cwd=str(other))
    assert result is not None
    body = result.get("hookSpecificOutput", result)
    assert body["permissionDecisionReason"].startswith(DENY_REASON)
