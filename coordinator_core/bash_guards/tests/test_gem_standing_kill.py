"""Tests for the shared G-EM-standing kill verdict."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _gem_cross_repo_commit, _gem_standing_kill as gsk


@pytest.fixture
def holder(monkeypatch):
    calls = []

    def fake(session_id, repo_root):
        calls.append((session_id, repo_root))
        return session_id == "gem-1"

    monkeypatch.setattr(_gem_cross_repo_commit, "session_holds_live_gem_standing", fake)
    return calls


def test_gem_main_session_has_standing(holder):
    assert gsk.gem_standing({"session_id": "gem-1", "cwd": "/r"}) is True
    assert holder == [("gem-1", "/r")]


def test_other_session_has_no_standing(holder):
    assert gsk.gem_standing({"session_id": "em-2", "cwd": "/r"}) is False


def test_subagent_of_gem_session_has_no_standing(holder):
    assert gsk.gem_standing({"session_id": "gem-1", "cwd": "/r", "agent_id": "a1"}) is False
    assert holder == []


def test_missing_fields_and_exceptions_are_false(monkeypatch):
    assert gsk.gem_standing({}) is False

    def boom(*a):
        raise RuntimeError

    monkeypatch.setattr(_gem_cross_repo_commit, "session_holds_live_gem_standing", boom)
    assert gsk.gem_standing({"session_id": "gem-1", "cwd": "/r"}) is False


def test_command_shape_predicate_is_gone():
    assert not hasattr(gsk, "is_gem_save_box")
