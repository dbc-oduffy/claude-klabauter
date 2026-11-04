"""Tests for the durable roster and BridgeMessage contract."""
from __future__ import annotations

import json

import pytest

from coordinator_core.agent_pipe import roster
from coordinator_core.agent_pipe.contract import BridgeMessage, PipeMessage, roster_path, routes_path


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


def test_paths_under_settings_home(tmp_path):
    assert roster_path() == tmp_path / "agent-pipe" / "roster.json"
    assert routes_path() == tmp_path / "agent-pipe" / "routes.json"


def test_resolve_unbound_is_none_and_list_empty():
    assert roster.resolve("nobody") is None
    assert roster.list() == {}


def test_bind_then_rebind_resolves_to_new_id():
    roster.bind("em", "claude", "sess-1", token="tok-1")
    roster.bind("em", "claude", "sess-2", token="tok-2")
    b = roster.resolve("em")
    assert b["id"] == "sess-2" and b["token"] == "tok-2"
    assert list(roster.list()) == ["em"]


def test_codex_binding_carries_argv_and_token_optional():
    roster.bind("cx", "codex", "thr-1", argv=["codex", "app-server"])
    b = roster.resolve("cx")
    assert b["argv"] == ["codex", "app-server"] and b["token"] is None


def test_bind_rejects_bad_kind_name_and_id():
    with pytest.raises(ValueError):
        roster.bind("x", "slack", "1")
    with pytest.raises(ValueError):
        roster.bind("../x", "claude", "1")
    with pytest.raises(ValueError):
        roster.bind("x", "claude", "")


def test_write_is_atomic_json_and_leaves_no_temp(tmp_path):
    roster.bind("a", "agy", "c1")
    assert json.loads(roster_path().read_text(encoding="utf-8"))["a"]["kind"] == "agy"
    assert [p.name for p in roster_path().parent.iterdir()] == ["roster.json"]


def test_bridge_message_line_labels_and_drops_none():
    m = BridgeMessage("m1", "em", "claude", "cx", "hi\nthere", "2026-11-04T00:00:00Z", origin="bridge", label="from em (claude)")
    d = json.loads(m.to_json_line())
    assert d["label"] == "from em (claude)" and d["content"] == "hi\nthere" and "\n" not in m.to_json_line()
    bare = json.loads(BridgeMessage("m2", "em", "claude", "cx", "x", "t").to_json_line())
    assert "origin" not in bare and "label" not in bare


def test_pipe_message_line_unchanged():
    line = PipeMessage("m", "antigravity", "c", "s", "x", "t").to_json_line()
    assert line == '{"id":"m","sender_agent":"antigravity","sender_conversation":"c","to_session":"s","content":"x","ts":"t"}'
