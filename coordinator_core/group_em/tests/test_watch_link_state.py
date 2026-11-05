"""Link-state records written by unimatrix-roster surface as one LINK-DOWN / LINK-UP line per transition."""

import json
import os

from coordinator_core.group_em import watch


def _write(home, alias, state, reason=""):
    d = watch.link_state_dir(str(home))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{alias}.json"), "w", encoding="utf-8") as fh:
        json.dump({"alias": alias, "state": state, "since": "2026-11-05T20:20:00Z", "reason": reason}, fh)


def _ticks(home, n, prev, lines):
    for _ in range(n):
        prev = watch.link_tick(prev, lines.append, str(home))
    return prev


def test_one_down_and_one_up_across_many_ticks(tmp_path):
    lines = []
    prev = _ticks(tmp_path, 2, None, lines)
    assert lines == []
    _write(tmp_path, "machine-b", "down", "timed out")
    prev = _ticks(tmp_path, 5, prev, lines)
    assert lines == ["LINK-DOWN machine-b: timed out"]
    _write(tmp_path, "machine-b", "up")
    prev = _ticks(tmp_path, 5, prev, lines)
    assert lines == ["LINK-DOWN machine-b: timed out", "LINK-UP machine-b"]


def test_already_down_on_first_tick_is_reported_once_up_is_not(tmp_path):
    _write(tmp_path, "a", "down", "x")
    _write(tmp_path, "b", "up")
    lines = []
    _ticks(tmp_path, 3, None, lines)
    assert lines == ["LINK-DOWN a: x"]


def test_missing_and_corrupt_records_are_skipped(tmp_path):
    d = watch.link_state_dir(str(tmp_path))
    os.makedirs(d)
    with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    with open(os.path.join(d, "odd.json"), "w", encoding="utf-8") as fh:
        json.dump({"state": "sideways"}, fh)
    lines = []
    assert _ticks(tmp_path, 2, None, lines) == {}
    assert lines == []
    assert watch.read_link_states(str(tmp_path / "absent")) == {}


def test_a_vanished_record_is_not_a_transition(tmp_path):
    _write(tmp_path, "a", "down")
    lines = []
    prev = _ticks(tmp_path, 1, None, lines)
    os.remove(os.path.join(watch.link_state_dir(str(tmp_path)), "a.json"))
    prev = _ticks(tmp_path, 2, prev, lines)
    assert lines == ["LINK-DOWN a"]
    assert prev == {"a": "down"}


def test_tick_once_carries_link_state_across_wakes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(watch, "poll_once", lambda *a, **k: ({}, [], {}, 0))
    monkeypatch.setattr(watch, "_load_alarm_tick", lambda *a, **k: None)
    monkeypatch.setattr(watch, "_memory_tick", lambda *a, **k: None)
    monkeypatch.setattr(watch.watch_heartbeat, "stamp", lambda *a, **k: True)
    monkeypatch.setattr(watch, "_prune_spool", lambda *a: None)
    repo = tmp_path / "repo"
    repo.mkdir()

    class Out:
        def __init__(self):
            self.text = ""

        def write(self, s):
            self.text += s

        def flush(self):
            pass

    out = Out()
    wake = lambda: watch.tick_once(str(repo), "c", "g", stream=out)
    assert wake() == 0
    _write(home, "far", "down", "boom")
    assert wake() == 0 and wake() == 0
    _write(home, "far", "up")
    assert wake() == 0 and wake() == 0
    link_lines = [ln for ln in out.text.splitlines() if ln.startswith("LINK-")]
    assert link_lines == ["LINK-DOWN far: boom", "LINK-UP far"]
