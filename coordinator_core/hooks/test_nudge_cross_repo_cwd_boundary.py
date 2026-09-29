from __future__ import annotations

from coordinator_core.hooks import nudge_cross_repo_cwd_boundary as m


def test_crossing_into_sibling_root_emits_advisory(monkeypatch):
    monkeypatch.setattr(
        m, "coordinator_engine_root_with_class", lambda: ("/repo/claude-klabauter", "resolved_engine")
    )
    result = m._handler({"old_cwd": "/repo/other", "new_cwd": "/repo/claude-klabauter/sub"})
    assert result["hookSpecificOutput"]["hookEventName"] == "CwdChanged"
    assert "cross-repo boundary" in result["hookSpecificOutput"]["additionalContext"]


def test_unresolved_sibling_is_a_no_op(monkeypatch):
    monkeypatch.setattr(
        m, "coordinator_engine_root_with_class", lambda: (None, "unresolved")
    )
    assert m._handler({"old_cwd": "/a", "new_cwd": "/b"}) == {}


def test_non_crossing_move_is_a_no_op(monkeypatch):
    monkeypatch.setattr(
        m, "coordinator_engine_root_with_class", lambda: ("/repo/claude-klabauter", "resolved_engine")
    )
    assert m._handler({"old_cwd": "/repo/other/a", "new_cwd": "/repo/other/b"}) == {}
