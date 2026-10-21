"""Tests for the per-send record module (write, bounded view, migration)."""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from coordinator_core.ops.fleet import memo_sent_records as msr
from coordinator_core.session import machinery_paths

NOW = datetime.datetime(2026, 10, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)
_ILLEGAL = set('<>:"/\\|?*')


def _row(when: datetime.datetime, topic: str = "t", sent_by: str = "abcdef12-0000") -> dict:
    return {
        "sent_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": "r", "topic": topic, "kind": "fyi", "summary": "s",
        "delivered_to": f"inbox/{topic}.md", "in_reply_to": None,
        "delivery_commit_sha": "0" * 40, "sent_by": sent_by,
    }


def _write_legacy(path: Path, rows: list[dict]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r) + "\n" for r in rows).encode()
    path.write_bytes(data)
    return data


def test_record_relpath_shape_has_no_windows_illegal_characters():
    rel = msr.record_relpath(_row(NOW, topic="a b:c/d?", sent_by="unresolved"))
    assert rel == "sent-records/2026-10/20261021T120000Z-a-b-c-d-unattrib.json"
    assert not (_ILLEGAL & set(rel.replace("/", "")))
    assert msr.record_relpath(_row(NOW)).endswith("-t-abcdef12.json")


def test_write_record_is_write_once_with_collision_suffix(tmp_path):
    p1 = msr.write_record(tmp_path, _row(NOW))
    p2 = msr.write_record(tmp_path, _row(NOW))
    p3 = msr.write_record(tmp_path, _row(NOW))
    assert [p.name[-7:] for p in (p1, p2, p3)] == ["12.json", "-2.json", "-3.json"]
    assert json.loads(p1.read_text()) == _row(NOW)
    assert not list(p1.parent.glob(".*.tmp"))
    assert b"\r" not in p1.read_bytes()


def test_view_opens_exactly_view_max_rows_of_300(tmp_path, monkeypatch):
    for i in range(300):
        msr.write_record(tmp_path, _row(NOW - datetime.timedelta(seconds=i), topic=f"t{i}"))
    opened: list[Path] = []
    real = Path.read_text

    def spy(self, *a, **k):
        opened.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    rows = msr.iter_view(tmp_path, now=NOW)
    assert len([p for p in opened if p.suffix == ".json"]) == msr.VIEW_MAX_ROWS == 250
    assert len(rows) == 250
    assert rows[-1]["topic"] == "t0"  # oldest-first, newest last
    assert rows[0]["topic"] == "t249"


def test_view_age_bound_is_by_filename_and_spans_previous_month(tmp_path):
    msr.write_record(tmp_path, _row(NOW - datetime.timedelta(days=20), "recent"))
    msr.write_record(tmp_path, _row(NOW - datetime.timedelta(days=31), "old"))
    assert [r["topic"] for r in msr.iter_view(tmp_path, now=NOW)] == ["recent"]


def test_malformed_files_and_lines_are_skipped(tmp_path):
    good = msr.write_record(tmp_path, _row(NOW, "good"))
    (good.parent / "20261021T120001Z-bad-x.json").write_text("{nope", encoding="utf-8")
    (good.parent / "20261021T120002Z-list-x.json").write_text("[1]", encoding="utf-8")
    ledger = Path(machinery_paths.memo_outbox_sent_ledger_path(str(tmp_path)))
    ledger.write_text("garbage\n\n[1]\n", encoding="utf-8")
    assert [r["topic"] for r in msr.iter_view(tmp_path, now=NOW)] == ["good"]


def test_legacy_rows_union_in_and_dedup_against_records(tmp_path):
    dup = _row(NOW - datetime.timedelta(hours=1), "dup")
    msr.write_record(tmp_path, dup)
    live = Path(machinery_paths.memo_outbox_sent_ledger_path(str(tmp_path)))
    _write_legacy(live, [dup, _row(NOW - datetime.timedelta(hours=2), "live-only")])
    _write_legacy(
        tmp_path / "state" / "memo-outbox" / "sent-ledger.jsonl",
        [_row(NOW - datetime.timedelta(hours=3), "state-only")],
    )
    topics = [r["topic"] for r in msr.iter_view(tmp_path, now=NOW)]
    assert topics == ["state-only", "live-only", "dup"]


def test_migration_is_idempotent_and_moves_bytes_unchanged(tmp_path):
    live = Path(machinery_paths.memo_outbox_sent_ledger_path(str(tmp_path)))
    state = tmp_path / "state" / "memo-outbox" / "sent-ledger.jsonl"
    rows = [_row(NOW - datetime.timedelta(minutes=i), f"m{i}") for i in range(3)]
    live_bytes = _write_legacy(live, rows[:2])
    state_bytes = _write_legacy(state, rows[2:])
    dry = msr.migrate_legacy_ledgers(tmp_path, dry_run=True)
    assert dry == {"written": 3, "skipped": 0, "moved": 2} and live.exists()
    assert msr.migrate_legacy_ledgers(tmp_path, dry_run=False) == {
        "written": 3, "skipped": 0, "moved": 2,
    }
    retired = live.parent / "retired-ledgers"
    assert (retired / "sent-ledger.jsonl").read_bytes() == live_bytes
    assert (retired / "state-memo-outbox-sent-ledger.jsonl").read_bytes() == state_bytes
    assert not live.exists() and not state.exists()
    assert msr.migrate_legacy_ledgers(tmp_path, dry_run=False) == {
        "written": 0, "skipped": 3, "moved": 0,
    }
    assert len(msr.iter_view(tmp_path, now=NOW)) == 3


def test_migration_lifts_a_row_appended_to_a_retired_ledger(tmp_path):
    live = Path(machinery_paths.memo_outbox_sent_ledger_path(str(tmp_path)))
    _write_legacy(live, [_row(NOW, "a")])
    msr.migrate_legacy_ledgers(tmp_path, dry_run=False)
    retired = live.parent / "retired-ledgers" / "sent-ledger.jsonl"
    with open(retired, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_row(NOW - datetime.timedelta(minutes=5), "late")) + "\n")
    assert msr.migrate_legacy_ledgers(tmp_path, dry_run=False)["written"] == 1
    # iter_view never reads the retired target
    topics = {r["topic"] for r in msr.iter_view(tmp_path, now=NOW)}
    assert topics == {"a", "late"}


def test_two_sender_record_sets_merge_into_one_view(tmp_path):
    msr.write_record(tmp_path, _row(NOW, "same", sent_by="aaaaaaaa-1"))
    msr.write_record(tmp_path, _row(NOW, "same", sent_by="bbbbbbbb-2"))
    assert len(msr.iter_view(tmp_path, now=NOW)) == 2
