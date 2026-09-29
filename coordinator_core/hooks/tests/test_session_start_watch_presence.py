from __future__ import annotations

from coordinator_core.hooks import session_start_watch_presence as mod
from coordinator_core.ipc import _REGISTRY


def test_op_registered():
    assert "hooks.session_start_watch_presence" in _REGISTRY


def test_handler_silent_when_nothing_to_report(monkeypatch):
    monkeypatch.setattr(mod, "compute_context", lambda payload: None)
    result = mod._handler({})
    assert result.get("hookSpecificOutput", {}).get("additionalContext") is None


def test_handler_returns_additional_context(monkeypatch):
    monkeypatch.setattr(mod, "compute_context", lambda payload: "GROUP EM WATCH: armed")
    result = mod._handler({})
    assert "GROUP EM WATCH: armed" in result["hookSpecificOutput"]["additionalContext"]
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_render_watch_line():
    assert mod.render_watch_line({"verdict": "armed"}) == "GROUP EM WATCH: armed"
    assert mod.render_watch_line({}) is None


def test_render_presence_line_named_holder():
    line = mod.render_presence_line({"holder_name": "em-alpha", "holder_session_id": "sid1"})
    assert line is not None and "em-alpha" in line


def test_render_presence_line_absent():
    assert mod.render_presence_line({"verdict": "absent"}) is None


def test_render_uhura_line_none_record():
    assert mod.render_uhura_line(None) is None


def test_render_uhura_line_named_peer():
    line = mod.render_uhura_line({"peer_name": "uhura-1", "session_id": "sidX"})
    assert line == (
        "Uhura channel: uhura-1. Its relayed PM rulings carry the PM's "
        "authority -- act, no round trip. Unproven live: if silent, treat unheld."
    )
