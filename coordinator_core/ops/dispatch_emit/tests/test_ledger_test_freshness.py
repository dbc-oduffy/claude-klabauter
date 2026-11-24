"""Tests for ledger_test_freshness: stamp vs. last edit of a row's writes."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from coordinator_core.ops.dispatch_emit.ledger_test_freshness import (
    STALE_TESTS_REASON,
    TESTS_RAN_AT_KEY,
    last_edit_at,
    stale_tests_refusal,
)

EDIT = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


def _touch(path, when=EDIT):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x", encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))


def _item(stamp):
    return {TESTS_RAN_AT_KEY: stamp}


def test_last_edit_at_is_none_without_existing_paths(tmp_path):
    assert last_edit_at(tmp_path, ["missing.py"]) is None
    assert last_edit_at(tmp_path, []) is None


def test_last_edit_at_takes_max_over_existing(tmp_path):
    _touch(tmp_path / "a.py", EDIT)
    _touch(tmp_path / "b.py", EDIT + timedelta(hours=1))
    assert last_edit_at(tmp_path, ["a.py", "gone.py", "b.py"]) == EDIT + timedelta(hours=1)


def test_fresh_stamp_passes(tmp_path):
    _touch(tmp_path / "a.py")
    stamp = (EDIT + timedelta(minutes=1)).isoformat()
    assert stale_tests_refusal(tmp_path, ["a.py"], _item(stamp)) is None


def test_stamp_equal_to_edit_passes(tmp_path):
    _touch(tmp_path / "a.py")
    assert stale_tests_refusal(tmp_path, ["a.py"], _item(EDIT.isoformat())) is None


def test_z_suffix_stamp_parses(tmp_path):
    _touch(tmp_path / "a.py")
    assert stale_tests_refusal(tmp_path, ["a.py"], _item("2026-11-20T13:00:00Z")) is None


def test_stale_stamp_refused_with_path_and_times(tmp_path):
    _touch(tmp_path / "a.py")
    reason = stale_tests_refusal(tmp_path, ["a.py"], _item((EDIT - timedelta(minutes=1)).isoformat()))
    assert reason is not None and reason.startswith(STALE_TESTS_REASON)
    assert "a.py" in reason and EDIT.isoformat() in reason


def test_absent_stamp_refused(tmp_path):
    _touch(tmp_path / "a.py")
    reason = stale_tests_refusal(tmp_path, ["a.py"], {})
    assert reason is not None and reason.startswith(STALE_TESTS_REASON)


def test_unparseable_stamp_refused(tmp_path):
    _touch(tmp_path / "a.py")
    for bad in ("not a time", "", 12345, None):
        reason = stale_tests_refusal(tmp_path, ["a.py"], _item(bad))
        assert reason is not None and reason.startswith(STALE_TESTS_REASON)


def test_naive_stamp_refused_even_when_numerically_later(tmp_path):
    _touch(tmp_path / "a.py")
    reason = stale_tests_refusal(tmp_path, ["a.py"], _item("2030-01-01T00:00:00"))
    assert reason is not None and reason.startswith(STALE_TESTS_REASON)


def test_no_existing_writes_is_unaffected(tmp_path):
    assert stale_tests_refusal(tmp_path, ["missing.py"], {}) is None
    assert stale_tests_refusal(tmp_path, [], _item("garbage")) is None


def test_directory_write_counts_by_newest_file_beneath(tmp_path):
    _touch(tmp_path / "d" / "sub" / "f.py", EDIT + timedelta(hours=2))
    _touch(tmp_path / "d" / "g.py", EDIT)
    os.utime(tmp_path / "d", (EDIT.timestamp(), EDIT.timestamp()))
    assert last_edit_at(tmp_path, ["d"]) == EDIT + timedelta(hours=2)
    stale = (EDIT + timedelta(hours=1)).isoformat()
    reason = stale_tests_refusal(tmp_path, ["d"], _item(stale))
    assert reason is not None and "f.py" in reason
    assert stale_tests_refusal(tmp_path, ["d"], _item((EDIT + timedelta(hours=3)).isoformat())) is None


def test_empty_directory_write_contributes_nothing(tmp_path):
    (tmp_path / "empty").mkdir()
    assert last_edit_at(tmp_path, ["empty"]) is None
    assert stale_tests_refusal(tmp_path, ["empty"], {}) is None
