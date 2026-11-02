"""Request-path reads in the halted-withheld index and the UE marker walk stop at the read deadline."""
from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit import ue_route_source as ue


class _Clock:
    """Advances `step` per reading; expiry is deterministic."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _seed_index(tmp_path, n=3):
    index = tmp_path / idx.INDEX_DIR
    index.mkdir(parents=True)
    (index / idx.SENTINEL).write_bytes(b"")
    for i in range(n):
        (index / f"run{i}.json").write_text(json.dumps({"paths": [f"p{i}"], "at_ns": 1}), encoding="utf-8")


def test_entries_read_every_file_when_healthy(tmp_path):
    _seed_index(tmp_path)
    assert sorted(idx.entries(tmp_path)) == ["run0", "run1", "run2"]


def test_entries_stop_parsing_once_the_deadline_passes(tmp_path, monkeypatch):
    _seed_index(tmp_path)
    parsed: list = []
    real = idx._parse_file

    def slow(file):
        parsed.append(file)
        return real(file)

    monkeypatch.setattr(idx, "_parse_file", slow)
    monkeypatch.setattr(idx, "_clock", _Clock(idx.READ_DEADLINE_SECS * 0.6))
    got = idx.entries(tmp_path)
    assert len(parsed) < 3
    assert len(got) == len(parsed)


def test_run_ids_stop_at_the_deadline(tmp_path, monkeypatch):
    _seed_index(tmp_path)
    monkeypatch.setattr(idx, "_clock", _Clock(idx.READ_DEADLINE_SECS * 10))
    assert idx.run_ids(tmp_path) == []


def _tree(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "G.uproject").write_text("{}", encoding="utf-8")


def test_marker_found_when_healthy(tmp_path):
    _tree(tmp_path)
    assert ue._has_ue_marker(str(tmp_path), 0) is True


def test_marker_walk_returns_false_past_the_deadline(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(ue, "_clock", _Clock(ue.READ_DEADLINE_SECS * 10))
    assert ue._has_ue_marker(str(tmp_path), 0) is False


def test_marker_walk_neither_reads_past_nor_descends_an_oversized_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ue, "_MAX_DIR_ENTRIES", 4)
    data = tmp_path / "raw_sources"
    data.mkdir()
    for i in range(5):
        (data / f"p{i}.jpg").write_text("")
    (data / "deep").mkdir()
    (data / "deep" / "Game.uproject").write_text("")
    assert ue._has_ue_marker(str(tmp_path), 0) is False


def test_marker_walk_stops_at_the_total_entry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ue, "_MAX_TOTAL_ENTRIES", 3)
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    (tmp_path / "c" / "Game.uproject").write_text("")
    assert ue._has_ue_marker(str(tmp_path), 0) is False


def test_marker_in_a_small_tree_still_found(tmp_path):
    (tmp_path / "Plugins" / "P").mkdir(parents=True)
    (tmp_path / "Plugins" / "P" / "P.uplugin").write_text("")
    assert ue._has_ue_marker(str(tmp_path), 0) is True
