from __future__ import annotations

import json

from coordinator_core.session import external_registry
from coordinator_core.tui_status import fields
from coordinator_core.tui_status.fields import format_line, resolve_name


def peers(tmp_path, monkeypatch, rows):
    path = tmp_path / "unimatrix" / "peers.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"peers": rows}), encoding="utf-8")
    monkeypatch.setattr(fields, "peers_path", lambda: path)
    monkeypatch.setattr(external_registry, "lookup", lambda sid: None)


def test_single_match_returns_name(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "grok", "target": "s1", "name": "g-one"},
                                  {"vendor": "codex", "target": "s1", "name": "c-one"}])
    assert resolve_name("grok", ["s1"]) == "g-one"
    assert resolve_name("grok", ["x", "s1"]) == "g-one"


def test_zero_or_ambiguous_match_is_none(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "agy", "target": "s1", "name": "a"},
                                  {"vendor": "agy", "target": "s1", "name": "b"}])
    assert resolve_name("agy", ["s1"]) is None
    assert resolve_name("agy", ["nope"]) is None
    assert resolve_name("agy", []) is None


def test_registry_preference_breaks_ambiguity_only_for_a_matching_name(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "codex", "target": "s1", "name": "a"},
                                  {"vendor": "codex", "target": "s1", "name": "b"}])
    monkeypatch.setattr(external_registry, "lookup", lambda sid: {"address_version": 1, "name": "b"})
    assert resolve_name("codex", ["s1"]) == "b"
    monkeypatch.setattr(external_registry, "lookup", lambda sid: {"address_version": 1, "name": "zzz"})
    assert resolve_name("codex", ["s1"]) is None
    monkeypatch.setattr(external_registry, "lookup", lambda sid: {"address_version": 2, "name": "b"})
    assert resolve_name("codex", ["s1"]) is None


def test_missing_or_bad_peers_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(fields, "peers_path", lambda: tmp_path / "absent.json")
    assert resolve_name("grok", ["s1"]) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setattr(fields, "peers_path", lambda: bad)
    assert resolve_name("grok", ["s1"]) is None


def test_format_line_unknown_slots_never_zero_or_full():
    line = format_line(None, None, None)
    assert line == "unknown · unknown · unknown"
    assert "0" not in line and "▰" not in line


def test_format_line_observed_values_and_middle():
    line = format_line("me", 40, 1.5, middle=("gpt", "high"))
    assert line == "me · gpt · high · ▰▰▱▱▱ 40% ctx · ~$1.50"
    assert format_line("me", 0, 0.0).endswith("▱▱▱▱▱ 0% ctx · ~$0.00")


def test_format_line_partial_marks_cost_only_when_observed():
    assert format_line("me", None, 2, partial=True).endswith("~$2.00+ [coverage incomplete]")
    assert "incomplete" not in format_line("me", None, None, partial=True)
