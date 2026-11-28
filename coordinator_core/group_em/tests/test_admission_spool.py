"""Tests for coordinator_core.group_em.admission_spool; every case writes under tmp_path only."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.group_em import admission_post, admission_spool


def _admitted(tmp_path: Path) -> list:
    p = admission_post.admission_path(tmp_path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_paths_sit_under_the_box_dir(tmp_path: Path) -> None:
    assert admission_spool.spool_path(tmp_path) == tmp_path / "box" / "admission-spool.jsonl"
    assert admission_spool.cursor_path("Mini.local", tmp_path) == tmp_path / "box" / "admission-drain" / "mini.cursor"


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    assert admission_spool.spool_line({"phase": "START", "n": 1}, tmp_path) is None
    assert admission_spool.spool_line({"phase": "END", "n": 2}, tmp_path) is None
    r = admission_spool.read_since(0, tmp_path)
    assert r["ok"] and [json.loads(x)["n"] for x in r["lines"]] == [1, 2]
    assert r["next_offset"] == admission_spool.spool_path(tmp_path).stat().st_size
    again = admission_spool.read_since(r["next_offset"], tmp_path)
    assert again == {"ok": True, "lines": [], "next_offset": r["next_offset"]}


def test_spool_line_reports_a_failure_reason_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "box"
    blocker.write_text("x", encoding="utf-8")
    reason = admission_spool.spool_line({"a": 1}, tmp_path)
    assert isinstance(reason, str) and reason.startswith("write_failed")
    assert admission_spool.spool_line({"a": object()}, tmp_path).startswith("write_failed")


def test_byte_cap_returns_whole_lines_only(tmp_path: Path) -> None:
    for i in range(5):
        admission_spool.spool_line({"n": i}, tmp_path)
    first = admission_spool.read_since(0, tmp_path, max_bytes=20)
    assert first["lines"] and all(json.loads(x) for x in first["lines"])
    got = list(first["lines"])
    off = first["next_offset"]
    for _ in range(10):
        r = admission_spool.read_since(off, tmp_path, max_bytes=20)
        got += r["lines"]
        off = r["next_offset"]
    assert [json.loads(x)["n"] for x in got] == [0, 1, 2, 3, 4]


def test_torn_trailing_line_is_excluded(tmp_path: Path) -> None:
    admission_spool.spool_line({"n": 1}, tmp_path)
    path = admission_spool.spool_path(tmp_path)
    whole = path.stat().st_size
    with open(path, "ab") as fh:
        fh.write(b'{"n": 2')
    r = admission_spool.read_since(0, tmp_path)
    assert len(r["lines"]) == 1 and r["next_offset"] == whole


def test_offset_past_eof_restarts_at_zero(tmp_path: Path) -> None:
    admission_spool.spool_line({"n": 1}, tmp_path)
    r = admission_spool.read_since(10_000, tmp_path)
    assert [json.loads(x)["n"] for x in r["lines"]] == [1]


def test_missing_spool_and_oversize_line_never_raise(tmp_path: Path) -> None:
    assert admission_spool.read_since(0, tmp_path) == {"ok": True, "lines": [], "next_offset": 0}
    admission_spool.spool_line({"big": "x" * 200}, tmp_path)
    admission_spool.spool_line({"n": 2}, tmp_path)
    r = admission_spool.read_since(0, tmp_path, max_bytes=50)
    assert r["lines"] == [] and r["next_offset"] > 0
    r2 = admission_spool.read_since(r["next_offset"], tmp_path, max_bytes=50)
    assert [json.loads(x)["n"] for x in r2["lines"]] == [2]


def test_drain_appends_stamped_lines_and_advances_cursor(tmp_path: Path) -> None:
    src = tmp_path / "mini"
    admission_spool.spool_line({"phase": "START", "session_id": "s1"}, src)
    admission_spool.spool_line({"phase": "END", "session_id": "s1"}, src)
    reply = admission_spool.read_since(0, src)
    out = admission_spool.drain_into("mini", reply, tmp_path)
    assert out == {"appended": 2, "cursor": reply["next_offset"]}
    rows = _admitted(tmp_path)
    assert [r["phase"] for r in rows] == ["START", "END"] and all(r["drained_from"] == "mini" for r in rows)
    assert admission_spool.cursor_path("mini", tmp_path).read_text(encoding="ascii") == str(reply["next_offset"])


def test_drain_is_idempotent_on_a_repeat_reply(tmp_path: Path) -> None:
    src = tmp_path / "mini"
    admission_spool.spool_line({"phase": "START"}, src)
    reply = admission_spool.read_since(0, src)
    admission_spool.drain_into("mini", reply, tmp_path)
    again = admission_spool.drain_into("mini", reply, tmp_path)
    assert again["appended"] == 0 and len(_admitted(tmp_path)) == 1


def test_drain_continues_from_the_cursor_and_restarts_on_replaced_spool(tmp_path: Path) -> None:
    src = tmp_path / "mini"
    admission_spool.spool_line({"n": 1}, src)
    r1 = admission_spool.read_since(0, src)
    admission_spool.drain_into("mini", r1, tmp_path)
    admission_spool.spool_line({"n": 2}, src)
    r2 = admission_spool.read_since(r1["next_offset"], src)
    assert admission_spool.drain_into("mini", r2, tmp_path)["appended"] == 1
    admission_spool.spool_path(src).unlink()
    admission_spool.spool_line({"n": 3}, src)
    cursor = admission_spool.drain_into("mini", {"lines": [], "next_offset": 0, "restarted": True}, tmp_path)["cursor"]
    r3 = admission_spool.read_since(cursor, src)
    assert admission_spool.drain_into("mini", r3, tmp_path)["appended"] == 1
    assert [r["n"] for r in _admitted(tmp_path)] == [1, 2, 3]


def test_drain_skips_non_json_lines_and_stops_on_append_failure(tmp_path: Path, monkeypatch) -> None:
    reply = {"ok": True, "lines": ["not json", '{"n": 1}'], "next_offset": len("not json") + 1 + len('{"n": 1}') + 1}
    assert admission_spool.drain_into("mini", reply, tmp_path)["appended"] == 1
    fresh = tmp_path / "other"
    monkeypatch.setattr(admission_post, "_append_verified", lambda path, data: "short_write")
    out = admission_spool.drain_into("mini", {"lines": ['{"n": 2}'], "next_offset": 9}, fresh)
    assert out["appended"] == 0 and out["cursor"] == 0


def test_stale_reply_from_offset_zero_is_not_a_replaced_spool(tmp_path: Path) -> None:
    src = tmp_path / "mini"
    admission_spool.spool_line({"n": 1}, src)
    stale = admission_spool.read_since(0, src)
    admission_spool.spool_line({"n": 2}, src)
    fresh = admission_spool.read_since(stale["next_offset"], src)
    admission_spool.drain_into("mini", stale, tmp_path)
    admission_spool.drain_into("mini", fresh, tmp_path)
    again = admission_spool.drain_into("mini", stale, tmp_path)
    assert again["appended"] == 0 and [r["n"] for r in _admitted(tmp_path)] == [1, 2]
    assert "restarted" not in stale


def test_replaced_spool_reply_is_marked_restarted(tmp_path: Path) -> None:
    admission_spool.spool_line({"n": 1}, tmp_path)
    assert admission_spool.read_since(10_000, tmp_path)["restarted"] is True
