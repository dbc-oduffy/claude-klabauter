from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.session_baton import store


def _session(tmp_path: Path, monkeypatch, sid: str) -> Path:
    sdir = tmp_path / "coordinator-sessions" / sid
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "coordinator-sessions" / s)
    return sdir


def test_turns_append_in_order_verbatim_and_uncapped(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-pm")
    launch = "  build the thing\n\n" + "x" * 20000 + "  "
    assert PT.append_turn(launch, session_id="sid-pm")["turn"] == 0
    assert PT.append_turn("yes", session_id="sid-pm")["turn"] == 1
    assert PT.append_turn("yes", session_id="sid-pm")["turn"] == 2

    out = PT.read_turns(session_id="sid-pm")
    assert out["ok"] is True and out["count"] == 3
    assert out["text"] == launch
    assert not (sdir / store.BATON_FILENAME).exists()


def test_selectors(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-sel")
    for p in ("zero", "one", "two", "three"):
        PT.append_turn(p, session_id="sid-sel")

    assert PT.read_turns(session_id="sid-sel", turn=2)["text"] == "two"
    assert PT.read_turns(session_id="sid-sel", turn=-1)["text"] == "three"
    rng = PT.read_turns(session_id="sid-sel", start=1, end=2)
    assert [t["prompt"] for t in rng["turns"]] == ["one", "two"]
    assert rng["text"].startswith("## PM turn 1 (")
    assert len(PT.read_turns(session_id="sid-sel", all_turns=True)["turns"]) == 4
    assert PT.read_turns(session_id="sid-sel", start=3)["text"] == "three"
    miss = PT.read_turns(session_id="sid-sel", turn=9)
    assert miss["ok"] is False and miss["count"] == 4


def test_torn_line_keeps_its_index_and_is_skipped(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-torn")
    PT.append_turn("zero", session_id="sid-torn")
    with open(sdir / PT.PM_TURNS_FILENAME, "ab") as fh:
        fh.write(b'{"turn": 1, "ts": "x", "prom')
    assert PT.append_turn("two", session_id="sid-torn")["turn"] == 2

    out = PT.read_turns(session_id="sid-torn", all_turns=True)
    assert [t["turn"] for t in out["turns"]] == [0, 2]
    assert PT.read_turns(session_id="sid-torn", turn=2)["text"] == "two"


def test_entries_are_never_rewritten(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-ao")
    PT.append_turn("first", session_id="sid-ao")
    before = (sdir / PT.PM_TURNS_FILENAME).read_bytes()
    PT.append_turn("second", session_id="sid-ao")
    after = (sdir / PT.PM_TURNS_FILENAME).read_bytes()
    assert after.startswith(before)
    assert json.loads(after.splitlines()[1])["prompt"] == "second"


def test_missing_session_dir_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "absent" / s)
    out = PT.append_turn("hello", session_id="sid-none")
    assert out["ok"] is False and "no session directory" in out["reason"]
    assert not (tmp_path / "absent").exists()


def test_rejects_empty_prompt_and_bad_selector(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-bad")
    assert PT.append_turn("   ", session_id="sid-bad")["ok"] is False
    out = PT._pm_turns({"session_id": "sid-bad", "turn": "2"})
    assert out["ok"] is False and "integer" in out["reason"]


def test_recent_window_newest_first_whole_turns_within_budget(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-win")
    for p in ("a" * 50, "b" * 30, "c" * 30):
        PT.append_turn(p, session_id="sid-win")
    win = PT.recent_window(session_id="sid-win", budget=70)
    assert [e["turn"] for e in win] == [2, 1]
    assert "truncated" not in win[0]


def test_recent_window_cuts_an_oversized_newest_turn(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-big")
    PT.append_turn("small", session_id="sid-big")
    PT.append_turn("z" * 100, session_id="sid-big")
    win = PT.recent_window(session_id="sid-big", budget=40)
    assert win == [{"turn": 1, "ts": win[0]["ts"], "text": "z" * 40, "truncated": True}]
