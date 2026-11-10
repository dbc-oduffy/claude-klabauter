from __future__ import annotations

import json

from coordinator_core.session import external_registry
from coordinator_core.tui_status import agy, fields


def peers(tmp_path, monkeypatch, rows):
    path = tmp_path / "peers.json"
    path.write_text(json.dumps({"peers": rows}), encoding="utf-8")
    monkeypatch.setattr(fields, "peers_path", lambda: path)
    monkeypatch.setattr(external_registry, "lookup", lambda sid: None)


def test_name_by_session_or_conversation_id_cost_from_cost(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "agy", "target": "conv1", "name": "agy-one"}])
    out = agy.line({"session_id": "s1", "conversation_id": "conv1", "cost": 0.5})
    assert out == "agy-one · unknown · ~$0.50"


def test_window_figures_never_render_as_before_compact(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [])
    out = agy.line({"context_window_size": 1000, "used_percentage": 10, "remaining_percentage": 90})
    assert out == "unknown · unknown · unknown" and "ctx" not in out


def test_missing_zero_or_bool_cost_is_unknown(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [])
    for cost in (None, 0, 0.0, True, "1.0", {}):
        assert agy.line({"cost": cost}).endswith("unknown")


def test_ambiguous_match_prints_unknown(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "agy", "target": "s1", "name": "a"},
                                  {"vendor": "agy", "target": "c1", "name": "b"}])
    assert agy.line({"session_id": "s1", "conversation_id": "c1"}).startswith("unknown")
