"""Tests for `route_unreachable_signal` — the reader half of the
sanctioned-route ledger.

The properties worth pinning are not "does it format a string". They are the
ones that decide whether this section is still trustworthy on the day it
matters: it stays silent when nothing is wrong, it cannot be blinded by one bad
row in append-space shared by ~50 sessions, it forgets, and it never tells an
operator to re-run a mutation that may have landed.

The writer/reader spelling pin is the most load-bearing test here — the two
halves live in different planes and share no importable constant, so nothing
else would catch a reader quietly watching a path nobody writes.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

from coordinator_core.orientation.route_unreachable_signal import (
    LEDGER_RELPATH,
    RUNTIME_BASE_ENV,
    _TAIL_SCAN_BYTES,
    WINDOW_HOURS,
    emit_route_unreachable,
    ledger_path,
    read_recent_events,
)


@pytest.fixture(autouse=True)
def _base_in_tmp(tmp_path, monkeypatch):
    """The ledger is per-BOX, not per-repo, so isolation moves the runtime base
    rather than passing a root. Autouse: a test that forgets would append to the
    operator's real ledger."""
    monkeypatch.setenv(RUNTIME_BASE_ENV, str(tmp_path))
    return tmp_path

_NOW = datetime.datetime(2026, 9, 20, 18, 0, 0, tzinfo=datetime.timezone.utc)


def _write(repo_root: Path, rows: list[dict]) -> None:
    ledger = Path(ledger_path())
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )


def _row(**kw):
    row = {
        "arrival": "cold-spawn",
        "entrypoint": "coordinator-lesson-add.py",
        "op": "queue.append",
        "session": "sess-a",
        "ts": _NOW.isoformat(timespec="seconds"),
    }
    row.update(kw)
    return row


def test_the_writer_and_reader_agree_on_the_path():
    """`cc_invoke` writes this file and this module reads it, across a plane
    boundary with no shared constant — the transport deliberately carries no
    `coordinator_core` import. If either spelling moves, the reader watches a
    file nobody writes and renders silence that looks exactly like health."""
    lib_dir = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    import cc_invoke

    # FULL RESOLVED PATH, not the relpath. The first version of this test
    # compared the relative tuple and passed while the two halves wrote and
    # read different files: the path was repo-relative, the publish transform
    # rewrote the registry key anchoring it, and source and mirror resolved the
    # same tuple to different places. Equal spellings were never the property
    # worth pinning -- one file is.
    assert cc_invoke._route_unreachable_ledger_path() == ledger_path()
    assert cc_invoke._ROUTE_UNREACHABLE_LEDGER == LEDGER_RELPATH


def test_silent_when_the_ledger_is_absent(tmp_path):
    """The expected state on almost every session, forever. A box whose engine
    answers renders nothing here."""
    assert emit_route_unreachable(now=_NOW) == ""


def test_silent_when_every_event_is_outside_the_window(tmp_path):
    """It forgets. A line that kept rendering a bad afternoon weeks later would
    train operators to scroll past the section, which loses it entirely."""
    old = _NOW - datetime.timedelta(hours=WINDOW_HOURS + 1)
    _write(tmp_path, [_row(ts=old.isoformat(timespec="seconds"))])
    assert emit_route_unreachable(now=_NOW) == ""


def test_renders_the_op_and_the_count(tmp_path):
    _write(tmp_path, [_row(), _row(op="memo.draft")])
    line = emit_route_unreachable(now=_NOW)
    assert "2 delivered-but-unanswered" in line
    assert "`memo.draft`" in line
    assert "`queue.append`" in line


def test_distinct_sessions_are_reported_as_a_floor_not_a_total(tmp_path):
    """A session with no `CLAUDE_SESSION_ID` writes an empty id, so the count
    is a floor. Claiming it as exact would make the line disagree with what an
    operator can see running, and a surface that looks wrong gets ignored."""
    _write(tmp_path, [_row(session="a"), _row(session="b"), _row(session="")])
    line = emit_route_unreachable(now=_NOW)
    assert "at least 2 sessions" in line


def test_a_single_session_is_not_described_as_several(tmp_path):
    _write(tmp_path, [_row(session="a"), _row(session="a")])
    line = emit_route_unreachable(now=_NOW)
    assert "sessions" not in line


def test_it_never_tells_the_operator_to_re_run(tmp_path):
    """These are delivered-but-unanswered MUTATIONS. `cc_invoke.
    WarmDispatchIndeterminate`'s negative-spec forbids retrying them, and a
    start-up line reading as "these failed, try again" would invert it on the
    one surface everybody reads."""
    _write(tmp_path, [_row()])
    line = emit_route_unreachable(now=_NOW)
    assert "Do NOT re-run" in line
    assert "retry" not in line.lower()


def test_one_torn_row_does_not_blind_the_reader(tmp_path):
    """Append-space shared by ~50 concurrent sessions, written on an
    already-failing path. A malformed row is skipped, never fatal — otherwise
    a single tear hides every real event around it."""
    ledger = Path(ledger_path())
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(_row(op="a.op")) + "\n"
        + "{not json at all\n"
        + "[1,2,3]\n"
        + '"a bare string"\n'
        + json.dumps({"op": "no.ts"}) + "\n"
        + json.dumps(_row(op="b.op", ts="not-a-timestamp")) + "\n"
        + json.dumps(_row(op="c.op")) + "\n",
        encoding="utf-8",
    )
    events = read_recent_events(now=_NOW)
    assert [e["op"] for e in events] == ["a.op", "c.op"]


def test_a_naive_timestamp_is_read_as_utc(tmp_path):
    """The writer stamps tz-aware UTC, but a hand-added or older row may not.
    Treating a naive stamp as UTC keeps it in the window rather than silently
    discarding it — the alternative loses real events with no signal."""
    naive = _NOW.replace(tzinfo=None).isoformat(timespec="seconds")
    _write(tmp_path, [_row(ts=naive)])
    assert len(read_recent_events(now=_NOW)) == 1


def test_the_named_op_list_is_capped(tmp_path):
    _write(tmp_path, [_row(op=f"op.{i}") for i in range(9)])
    line = emit_route_unreachable(now=_NOW)
    assert "+5 more" in line


@pytest.mark.parametrize("bad", ["", "   ", "not json", "[]", "null"])
def test_a_ledger_of_only_garbage_is_silence_not_a_crash(tmp_path, bad):
    ledger = Path(ledger_path())
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(bad + "\n", encoding="utf-8")
    assert emit_route_unreachable(now=_NOW) == ""


def test_a_ledger_grown_past_the_tail_bound_still_renders_the_recent_window(tmp_path):
    """The reader scans a fixed tail, so an unpruned ledger cannot make a
    start-up path cost more every day — and must not cost it the recent rows
    either. Old rows beyond the bound are unreachable BY DESIGN: they are
    outside `WINDOW_HOURS` and would not render even if scanned."""
    import datetime as _dt

    now = _dt.datetime(2026, 9, 20, 12, 0, tzinfo=_dt.timezone.utc)
    stale = (now - _dt.timedelta(days=30)).isoformat(timespec="seconds")
    fresh = (now - _dt.timedelta(minutes=5)).isoformat(timespec="seconds")

    path = Path(ledger_path())
    path.parent.mkdir(parents=True, exist_ok=True)

    padding = json.dumps(
        {"arrival": "cold-spawn", "entrypoint": "old.py", "op": "old.op", "session": "s", "ts": stale},
        sort_keys=True,
    )
    rows = [padding] * ((_TAIL_SCAN_BYTES // len(padding)) + 50)
    rows.append(
        json.dumps(
            {"arrival": "warm-hit", "entrypoint": "cross-repo-memo.py", "op": "memo.draft", "session": "s1", "ts": fresh},
            sort_keys=True,
        )
    )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert path.stat().st_size > _TAIL_SCAN_BYTES

    events = read_recent_events(now=now)
    assert [e["op"] for e in events] == ["memo.draft"]

    line = emit_route_unreachable(now=now)
    assert "memo.draft" in line
    assert "Do NOT re-run" in line


def test_the_tail_seek_never_yields_a_torn_row(tmp_path):
    """A seek into the middle of the file lands mid-record. That fragment is
    dropped rather than parsed, so it can never surface as an event."""
    import datetime as _dt

    now = _dt.datetime(2026, 9, 20, 12, 0, tzinfo=_dt.timezone.utc)
    fresh = (now - _dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    path = Path(ledger_path())
    path.parent.mkdir(parents=True, exist_ok=True)

    row = json.dumps(
        {"arrival": "warm-hit", "entrypoint": "e.py", "op": "op.name", "session": "s", "ts": fresh},
        sort_keys=True,
    )
    filler = "x" * _TAIL_SCAN_BYTES
    path.write_text(filler + "\n" + row + "\n", encoding="utf-8")

    events = read_recent_events(now=now)
    assert [e["op"] for e in events] == ["op.name"]
