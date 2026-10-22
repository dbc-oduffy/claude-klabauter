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


def test_success_reply_reports_delivered_and_keeps_keys(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    r = admission_post.post("START", "s1", [os.getpid()], directory=tmp_path)
    assert r["delivered"] is True and "reason" not in r
    for key in ("posted", "path", "phase", "pids", "dead_pids", "holder_live"):
        assert key in r


def test_short_write_reports_not_delivered_loudly(tmp_path, monkeypatch, capsys):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, d: real(fd, d[: len(d) // 2]))
    r = admission_post.post("START", "s1", [], directory=tmp_path)
    assert r["delivered"] is False and r["posted"] is False and r["reason"] == "short_write"
    assert "NOT delivered" in capsys.readouterr().err


def test_write_oserror_reports_not_delivered(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})

    def boom(fd, d):
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", boom)
    r = admission_post.post("START", "s1", [], directory=tmp_path)
    assert r["delivered"] is False and r["reason"].startswith("write_failed")


def test_line_absent_at_path_reports_not_delivered(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    real = os.write

    def write_elsewhere(fd, d):
        real(fd, d)
        admission_post.admission_path(tmp_path).write_bytes(b"")
        return len(d)

    monkeypatch.setattr(os, "write", write_elsewhere)
    r = admission_post.post("START", "s1", [], directory=tmp_path)
    assert r["delivered"] is False and r["reason"] == "not_found_at_path"


def test_start_with_declared_peak_carries_field(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    admission_post.post("START", "s1", [], directory=tmp_path, declared_peak_mb=2048)
    (line,) = _lines(tmp_path)
    assert line["declared_peak_mb"] == 2048.0


def test_start_without_peak_and_end_omit_field(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    admission_post.post("START", "s1", [], directory=tmp_path)
    admission_post.post("END", "s1", [], directory=tmp_path, declared_peak_mb=2048)
    assert all("declared_peak_mb" not in x for x in _lines(tmp_path))


def test_invalid_peak_is_dropped_but_line_written(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    for bad in (-1, float("nan"), float("inf"), True, "5", admission_post.MAX_PEAK_MB * 2):
        admission_post.post("START", "s1", [], directory=tmp_path, declared_peak_mb=bad)
    lines = _lines(tmp_path)
    assert len(lines) == 6 and all("declared_peak_mb" not in x for x in lines)


def test_oversize_note_and_pid_list_are_capped(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    monkeypatch.setattr(admission_post.session_registry, "pid_alive", lambda pid: True)
    n = admission_post.MAX_PIDS + 10
    admission_post.post("START", "s1", range(1, n), note="x" * 5000, directory=tmp_path)
    (line,) = _lines(tmp_path)
    assert len(line["note"]) == admission_post.MAX_TEXT
    assert len(line["pids"]) == admission_post.MAX_PIDS
