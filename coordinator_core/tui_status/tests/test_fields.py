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


def test_grok_session_absent_from_peers_resolves_from_active_sessions(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [])
    home = tmp_path / "grokhome"
    home.mkdir()
    repo = tmp_path / "coordinator-content-repo"
    (repo / ".git").mkdir(parents=True)
    sid = "01a1c2d6-f3c4-7340-92bd-257341b50f06"
    (home / "active_sessions.json").write_text(json.dumps(
        [{"session_id": sid, "cwd": str(repo), "pid": 1}]), encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(home))
    name = resolve_name("grok", [sid])
    assert name is not None and name.startswith("grok-coordinator-content-repo-01a1c2d6-on-")
    assert resolve_name("grok", ["other"]) is None


def test_peers_row_wins_over_grok_session_source(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [{"vendor": "grok", "target": "s1", "name": "from-peers"}])
    home = tmp_path / "grokhome"
    home.mkdir()
    (home / "active_sessions.json").write_text(json.dumps(
        [{"session_id": "s1", "cwd": str(tmp_path), "pid": 1}]), encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(home))
    assert resolve_name("grok", ["s1"]) == "from-peers"


def test_agy_leg_record_resolves_roster_name(tmp_path, monkeypatch):
    peers(tmp_path, monkeypatch, [])
    legs = tmp_path / "unimatrix" / "agy-legs"
    legs.mkdir()
    repo = tmp_path / "coordinator-content-repo"
    (repo / ".git").mkdir(parents=True)
    conv = "11111111-2222-3333-4444-555555555555"
    (legs / f"{conv}.json").write_text(json.dumps(
        {"conversation": conv, "pid": 1, "cwd": str(repo)}), encoding="utf-8")
    name = resolve_name("agy", ["s-other", conv])
    assert name is not None and name.startswith("agy-coordinator-content-repo-") and "-on-" in name
    assert resolve_name("agy", ["99999999-2222-3333-4444-555555555555"]) is None


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
