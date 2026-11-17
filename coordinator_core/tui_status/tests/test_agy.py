from __future__ import annotations

import json

from coordinator_core.session import external_registry
from coordinator_core.tui_status import agy, fields
from pathlib import Path
import pytest


def peers(tmp_path, monkeypatch, rows) -> None:
    path = tmp_path / "peers.json"
    path.write_text(json.dumps({"peers": rows}), encoding="utf-8")
    monkeypatch.setattr(fields, "peers_path", lambda: path)
    monkeypatch.setattr(external_registry, "lookup", lambda sid: None)


def test_name_by_session_or_conversation_id_cost_from_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [{"vendor": "agy", "target": "conv1", "name": "agy-one"}])
    out = agy.line({"session_id": "s1", "conversation_id": "conv1", "cost": 0.5})
    assert out == "agy-one · unknown · ~$0.50"


def test_window_figures_never_render_as_before_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [])
    out = agy.line({"context_window_size": 1000, "used_percentage": 10, "remaining_percentage": 90})
    assert out == "unknown · unknown · unknown" and "ctx" not in out


def test_missing_zero_or_bool_cost_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [])
    for cost in (None, 0, 0.0, True, "1.0", {}):
        assert agy.line({"cost": cost}).endswith("unknown")


def test_leg_record_prints_roster_name_and_unknown_id_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [])
    legs = tmp_path / "agy-legs"
    legs.mkdir()
    conv = "11111111-2222-3333-4444-555555555555"
    (legs / f"{conv}.json").write_text(json.dumps({"conversation": conv, "pid": 1, "cwd": str(tmp_path)}),
                                       encoding="utf-8")
    out = agy.line({"session_id": "s1", "conversation_id": conv, "cost": 0.5})
    assert out.startswith(f"agy-{tmp_path.name}-") and out.endswith("unknown · ~$0.50")
    assert agy.line({"conversation_id": "22222222-2222-3333-4444-555555555555"}).startswith("unknown")


def test_ambiguous_match_prints_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [{"vendor": "agy", "target": "s1", "name": "a"},
                                  {"vendor": "agy", "target": "c1", "name": "b"}])
    assert agy.line({"session_id": "s1", "conversation_id": "c1"}).startswith("unknown")


PROBE = {
    "session_id": "s1", "conversation_id": "c1",
    "model": {"id": "Gemini 3.8 Flash (Medium)", "display_name": "Gemini 3.8 Flash (Medium)", "effort": "medium"},
    "context_window": {"total_input_tokens": 19482, "total_output_tokens": 33, "context_window_size": 1048576,
                       "used_percentage": 1.86, "remaining_percentage": 98.14,
                       "current_usage": {"input_tokens": 14555, "output_tokens": 33,
                                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
}


def test_probe_payload_prices_lifetime_tokens_partial_and_context_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [{"vendor": "agy", "target": "c1", "name": "agy-one"}])
    assert agy.line(PROBE) == "agy-one · unknown · ~$0.01+ [coverage incomplete]"


def test_unpriced_model_or_zero_tokens_stay_unknown_never_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [])
    other = {**PROBE, "model": {"id": "Gemini 4 Mystery"}}
    zero = {**PROBE, "context_window": {"total_input_tokens": 0, "total_output_tokens": 0}}
    no_model = {k: v for k, v in PROBE.items() if k != "model"}
    for payload in (other, zero, no_model, {**PROBE, "context_window": None}):
        assert agy.line(payload).endswith("unknown · unknown") and "$" not in agy.line(payload)


def test_payload_cost_still_wins_unmarked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peers(tmp_path, monkeypatch, [])
    assert agy.line({**PROBE, "cost": 0.5}).endswith("~$0.50")
