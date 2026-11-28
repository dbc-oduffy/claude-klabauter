"""admission_drain.drain_managed: holder-gated, cursor-driven drain of managed boxes' admission spools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.group_em import admission_drain, admission_post, admission_spool, box_registry, nomination, remote_roster, suite_grant

NOW = 1_000_000.0


def _spool_reply(lines: list, start: int = 0) -> dict:
    span = sum(len(x.encode("utf-8")) + 1 for x in lines)
    return {"ok": True, "lines": lines, "next_offset": start + span}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {"live": True, "boxes": [{"box_id": "mini"}, {"box_id": "me"}], "far": {}}
    monkeypatch.setattr(
        nomination, "box_holder", lambda directory=None, now=None: {"live": state["live"]} if state["live"] is not None else None
    )
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: list(state["boxes"]))
    monkeypatch.setattr(box_registry, "local_box_id", lambda home=None: "me")
    monkeypatch.setattr(
        suite_grant, "_far_box", lambda box_id, directory, home: (state["far"].get(box_id, {"box_id": box_id}), "")
    )
    return state


def _channel(tmp_path: Path) -> list:
    path = admission_post.admission_path(tmp_path)
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _line(run: str) -> str:
    return json.dumps({"phase": "START", "run": run, "pids": [4242]}, separators=(",", ":"))


def test_non_holder_is_a_noop_and_never_reads(tmp_path: Path, world: dict) -> None:
    world["live"] = None
    called = []
    assert admission_drain.drain_managed(tmp_path, reader=lambda b, o: called.append(b), now=NOW) == {}
    world["live"] = False
    assert admission_drain.drain_managed(tmp_path, reader=lambda b, o: called.append(b), now=NOW) == {}
    assert called == [] and _channel(tmp_path) == []


def test_holder_drains_managed_box_and_advances_cursor(tmp_path: Path, world: dict) -> None:
    offsets = []
    lines = [_line("a"), _line("b")]

    def reader(box: dict, offset: int) -> dict:
        offsets.append((box["box_id"], offset))
        return _spool_reply(lines, offset)

    out = admission_drain.drain_managed(tmp_path, reader=reader, now=NOW)
    assert out["mini"]["ok"] is True and out["mini"]["appended"] == 2
    assert [r["run"] for r in _channel(tmp_path)] == ["a", "b"]
    assert all(r["drained_from"] == "mini" for r in _channel(tmp_path))
    cursor = out["mini"]["cursor"]
    assert admission_spool._read_cursor("mini", tmp_path) == cursor > 0
    admission_drain.drain_managed(tmp_path, reader=reader, now=NOW + remote_roster.REFRESH_INTERVAL_S + 1)
    assert offsets == [("mini", 0), ("mini", cursor)]


def test_local_box_is_skipped(tmp_path: Path, world: dict) -> None:
    seen = []
    admission_drain.drain_managed(tmp_path, reader=lambda b, o: seen.append(b["box_id"]) or _spool_reply([], o), now=NOW)
    assert seen == ["mini"]


def test_unreachable_box_keeps_cursor_and_records_reason(tmp_path: Path, world: dict) -> None:
    ok = admission_drain.drain_managed(tmp_path, reader=lambda b, o: _spool_reply([_line("a")], o), now=NOW)
    cursor = ok["mini"]["cursor"]
    later = NOW + remote_roster.REFRESH_INTERVAL_S + 1
    out = admission_drain.drain_managed(
        tmp_path, reader=lambda b, o: {"ok": False, "reason": "ssh_timeout"}, now=later
    )
    assert out["mini"] == {"ok": False, "reason": "ssh_timeout", "cursor": cursor}
    raised = admission_drain.drain_managed(
        tmp_path, reader=lambda b, o: (_ for _ in ()).throw(OSError("down")), now=later + remote_roster.REFRESH_INTERVAL_S + 1
    )
    assert raised["mini"]["ok"] is False and raised["mini"]["reason"].startswith("read_failed:OSError")
    assert admission_spool._read_cursor("mini", tmp_path) == cursor
    assert len(_channel(tmp_path)) == 1


def test_unresolvable_box_reports_reason(tmp_path: Path, world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suite_grant, "_far_box", lambda box_id, directory, home: (None, "box_unknown"))
    out = admission_drain.drain_managed(tmp_path, reader=lambda b, o: _spool_reply([], o), now=NOW)
    assert out == {"mini": {"ok": False, "reason": "box_unknown"}}


def test_rate_limited_within_the_refresh_interval(tmp_path: Path, world: dict) -> None:
    calls = []
    reader = lambda b, o: calls.append(o) or _spool_reply([], o)  # noqa: E731
    admission_drain.drain_managed(tmp_path, reader=reader, now=NOW)
    assert admission_drain.drain_managed(tmp_path, reader=reader, now=NOW + 1) == {}
    assert len(calls) == 1
    admission_drain.drain_managed(tmp_path, reader=reader, now=NOW + remote_roster.REFRESH_INTERVAL_S)
    assert len(calls) == 2
