from __future__ import annotations

import json

from coordinator_core.session import external_registry
from coordinator_core.tui_status import fields, grok


def peers(tmp_path, monkeypatch, rows):
    path = tmp_path / "peers.json"
    path.write_text(json.dumps({"peers": rows}), encoding="utf-8")
    monkeypatch.setattr(fields, "peers_path", lambda: path)
    monkeypatch.setattr(external_registry, "lookup", lambda sid: None)


def test_full_payload_prints_name_before_compact_and_spend(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "grok", "target": "s1", "name": "grok-one"}])
    out = grok.line({"session_id": "s1", "context_window": {"remaining_percentage": 60},
                     "auto_compact_threshold_percent": 80, "cost": {"total_cost_usd": 1.234}})
    assert out.startswith("grok-one · ") and "40% ctx" in out and out.endswith("~$1.23")


def test_absent_keys_print_unknown_in_each_slot(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [])
    assert grok.line({}) == "unknown · unknown · unknown"


def test_window_without_threshold_is_unknown_not_before_compact(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [])
    out = grok.line({"session_id": "s1", "context_window": {"remaining_percentage": 90}})
    assert "ctx" not in out and out.count("unknown") == 3


def test_before_compact_clamps_at_zero():
    assert grok.before_compact(10, 80) == 0.0
    assert grok.before_compact(70, 80) == 50.0
    assert grok.before_compact(None, 80) is None
    assert grok.before_compact(50, None) is None
    assert grok.before_compact(True, 80) is None


def test_cost_below_display_floor_or_zero_is_unknown(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [])
    for cost in ({"total_cost_usd": 0.004}, {"total_cost_usd": 0}, {}, 3.0, None):
        assert grok.line({"cost": cost}).endswith("unknown")
    assert grok.line({"cost": {"total_cost_usd": 0.005}}).endswith("~$0.01")


def test_ambiguous_peer_rows_print_unknown_name(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "grok", "target": "s1", "name": "a"},
                                  {"vendor": "grok", "target": "s1", "name": "b"}])
    assert grok.line({"session_id": "s1"}).startswith("unknown")
