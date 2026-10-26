"""Sizing-keyed held-revision store: round trip, idempotent drain, torn line, no glob."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import held_revisions as hr

SIZING = "state/sizings/2026-10-26-x-slice-2.yaml"


def test_round_trip_and_drain(tmp_path):
    a = hr.hold(tmp_path, SIZING, "ask-1", text="recount", sender="peer")
    b = hr.hold(tmp_path, SIZING, "ask-1", note="check both arms")
    assert a["from"] == "peer" and "note" not in a and b["note"] == "check both arms"
    assert [e["id"] for e in hr.unconsumed(tmp_path, SIZING, "ask-other")] == [a["id"], b["id"]]
    drained = hr.drain(tmp_path, SIZING, "ask-2")
    assert [e["id"] for e in drained] == [a["id"], b["id"]]
    assert hr.unconsumed(tmp_path, SIZING) == []


def test_second_drain_is_idempotent(tmp_path):
    hr.hold(tmp_path, SIZING, "ask-1", text="t")
    assert len(hr.drain(tmp_path, SIZING, "ask-2")) == 1
    assert hr.drain(tmp_path, SIZING, "ask-3") == []


def test_pending_without_mark_stays_open(tmp_path):
    hr.hold(tmp_path, SIZING, "ask-1", text="t")
    assert len(hr.pending(tmp_path, SIZING)) == 1
    assert len(hr.unconsumed(tmp_path, SIZING)) == 1


def test_other_sizing_isolated_and_absent_file_empty(tmp_path):
    hr.hold(tmp_path, SIZING, "ask-1", text="t")
    assert hr.unconsumed(tmp_path, "state/sizings/other.yaml") == []
    assert hr.unconsumed(tmp_path / "nowhere", SIZING) == []


def test_torn_tail_line_skipped(tmp_path):
    e = hr.hold(tmp_path, SIZING, "ask-1", text="t")
    with hr.store_path(tmp_path, SIZING).open("a", encoding="utf-8") as fh:
        fh.write('{"id": "H-torn", "te')
    assert [x["id"] for x in hr.pending(tmp_path, SIZING)] == [e["id"]]


def test_path_computed_from_key_without_glob(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("glob used")

    monkeypatch.setattr(Path, "glob", boom)
    monkeypatch.setattr(Path, "rglob", boom)
    hr.hold(tmp_path, SIZING, "ask-1", text="t")
    assert len(hr.unconsumed(tmp_path, SIZING)) == 1
    assert hr.store_path(tmp_path, SIZING) == hr.store_path(tmp_path, SIZING.replace("/", "\\"))
    assert len(hr.store_path(tmp_path, SIZING).stem) == 16


def test_hold_requires_content(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        hr.hold(tmp_path, SIZING, "ask-1")
