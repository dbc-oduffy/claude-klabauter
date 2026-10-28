"""The write-guard deny seam hands the payload to annotate_deny: an EM-shaped
payload's deny carries the guard route key, a subagent-shaped one carries none."""
from __future__ import annotations

import copy

import pytest

from coordinator_core.guard_routes import keys
from coordinator_core.write_guards import engine

_GUARD = "fake-route-guard"
_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "deny",
        "permissionDecisionReason": "hard-deny for testing",
    }
}


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    folder = tmp_path / ".coordinator-claude-settings" / "coordinator-claude" / "docs" / "wiki" / "guard-routes"
    folder.mkdir(parents=True)
    (folder / keys.ROUTES_INDEX).write_text("# guard routes\n", encoding="utf-8")
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setattr(keys, "ROUTES_DIR_DISPLAY", str(folder))
    monkeypatch.setattr(
        engine,
        "_discover_guards",
        lambda: ([engine._Guard(_GUARD, "hard-deny", ["Write"], 5, lambda p: copy.deepcopy(_ENVELOPE))], []),
    )
    monkeypatch.setattr(engine, "_consume_unlock", lambda session_id, guard_name: False)
    monkeypatch.setattr(engine, "_record_deny_fire", lambda *a, **k: None)
    return tmp_path


def _payload(**extra):
    p = {
        "tool_name": "Write",
        "tool_input": {"file_path": "x.py"},
        "session_id": "sess-route-key",
    }
    p.update(extra)
    return p


def _reason(out):
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_em_payload_deny_carries_route_key(seeded):
    assert "gr:" + _GUARD in _reason(engine.evaluate(_payload()))


def test_subagent_payload_deny_carries_no_route(seeded):
    text = _reason(engine.evaluate(_payload(agent_id="a0123456789abcdef")))
    assert "gr:" not in text
    assert "guard-routes" not in text
