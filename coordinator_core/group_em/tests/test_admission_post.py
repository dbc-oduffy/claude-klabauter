"""Tests for coordinator_core.group_em.admission_post; every case writes under tmp_path only."""

from __future__ import annotations

import json
import os

from coordinator_core.group_em import admission_post, nomination


def _holder(monkeypatch, record):
    monkeypatch.setattr(nomination, "box_holder", lambda directory=None, now=None: record)


def _lines(tmp_path):
    p = admission_post.admission_path(tmp_path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_start_appends_one_line_with_live_pid(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    r = admission_post.post("start", "s1", [os.getpid()], run="probe", directory=tmp_path, now=5.0)
    assert r["posted"] and r["pids"] == [os.getpid()]
    (line,) = _lines(tmp_path)
    assert line["phase"] == "START" and line["session_id"] == "s1"
    assert line["run"] == "probe" and line["holder_session"] == "gem" and line["ts"] == 5.0


def test_end_appends_second_line(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    admission_post.post("START", "s1", [os.getpid()], directory=tmp_path)
    admission_post.post("END", "s1", [os.getpid()], directory=tmp_path)
    assert [x["phase"] for x in _lines(tmp_path)] == ["START", "END"]


def test_dead_pid_never_reported_live(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    monkeypatch.setattr(
        admission_post.session_registry, "pid_alive", lambda pid: pid == 111
    )
    r = admission_post.post("START", "s1", [111, 222, "x"], directory=tmp_path)
    assert r["pids"] == [111] and r["dead_pids"] == [222]
    (line,) = _lines(tmp_path)
    assert line["pids"] == [111] and line["dead_pids"] == [222]


def test_no_holder_fails_open_and_writes_nothing(tmp_path, monkeypatch):
    _holder(monkeypatch, None)
    r = admission_post.post("START", "s1", [os.getpid()], directory=tmp_path)
    assert r == {"posted": False, "reason": "no_box_holder"}
    assert not (tmp_path / "box").exists()


def test_holderless_start_written_when_holder_not_required(tmp_path, monkeypatch):
    _holder(monkeypatch, None)
    r = admission_post.post(
        "START", "s1", [os.getpid()], run="r", directory=tmp_path, require_holder=False
    )
    assert r["posted"] and r["holder_live"] is False
    (line,) = _lines(tmp_path)
    assert line["phase"] == "START" and line["holder_session"] is None


def test_bad_phase_refused(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    r = admission_post.post("PAUSE", "s1", [], directory=tmp_path)
    assert r == {"posted": False, "reason": "bad_phase"}
    assert not admission_post.admission_path(tmp_path).exists()


def test_oversize_note_and_pid_list_are_capped(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    monkeypatch.setattr(admission_post.session_registry, "pid_alive", lambda pid: True)
    n = admission_post.MAX_PIDS + 10
    admission_post.post("START", "s1", range(1, n), note="x" * 5000, directory=tmp_path)
    (line,) = _lines(tmp_path)
    assert len(line["note"]) == admission_post.MAX_TEXT
    assert len(line["pids"]) == admission_post.MAX_PIDS
