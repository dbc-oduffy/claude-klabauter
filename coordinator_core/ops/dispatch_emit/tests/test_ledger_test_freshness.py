"""Tests for ledger_test_freshness: stamp vs. last edit of a row's writes."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
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


RUN = "ask-20261120T000000-abc123"
LEG = EDIT + timedelta(minutes=10)


def _raw_blob(root, rel):
    data = (root / rel).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture(autouse=True)
def _hash_without_git(monkeypatch):
    monkeypatch.setattr(commit, "checkin_blob_sha", _raw_blob)


def _record(tmp_path, *, leg=LEG, rev=("a.py",), rev_at=EDIT):
    """The run's `test-leg.json` (measured at `leg`) and a `measured-rev.json` naming `rev` at their current bytes."""
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    if leg is not None:
        (run_dir / "test-leg.json").write_text(json.dumps({"measured_at": leg.isoformat()}), encoding="utf-8")
    if rev is not None:
        blobs = {rel: _raw_blob(tmp_path, rel) for rel in rev}
        record = run_dir / "measured-rev.json"
        record.write_text(json.dumps({"blobs": blobs, "paths": list(rev)}), encoding="utf-8")
        os.utime(record, (rev_at.timestamp(), rev_at.timestamp()))


def test_a_fresh_test_leg_stands_in_for_an_absent_tests_ran_at(tmp_path):
    _touch(tmp_path / "a.py")
    _record(tmp_path)
    assert stale_tests_refusal(tmp_path, ["a.py"], {}, RUN) is None


def test_the_leg_of_another_run_or_no_run_id_is_not_consulted(tmp_path):
    _touch(tmp_path / "a.py")
    _record(tmp_path)
    for run_id in (None, "ask-20261120T000000-other1"):
        reason = stale_tests_refusal(tmp_path, ["a.py"], {}, run_id)
        assert reason is not None and reason.startswith(STALE_TESTS_REASON)


def test_a_peer_touch_after_the_leg_without_a_content_change_is_not_an_edit(tmp_path):
    _touch(tmp_path / "a.py")
    _record(tmp_path)
    _touch(tmp_path / "a.py", LEG + timedelta(hours=1))
    assert stale_tests_refusal(tmp_path, ["a.py"], {}, RUN) is None


def test_the_runs_own_content_change_after_the_leg_is_refused(tmp_path):
    _touch(tmp_path / "a.py")
    _record(tmp_path)
    (tmp_path / "a.py").write_text("changed\n", encoding="utf-8")
    _touch(tmp_path / "a.py", LEG + timedelta(hours=1))
    reason = stale_tests_refusal(tmp_path, ["a.py"], {}, RUN)
    assert reason is not None and reason.startswith(STALE_TESTS_REASON) and "a.py" in reason


def test_a_newer_write_the_measurement_does_not_name_is_refused(tmp_path):
    _touch(tmp_path / "a.py")
    _touch(tmp_path / "b.py")
    _record(tmp_path, rev=("a.py",))
    _touch(tmp_path / "b.py", LEG + timedelta(hours=1))
    reason = stale_tests_refusal(tmp_path, ["a.py", "b.py"], {}, RUN)
    assert reason is not None and "b.py" in reason


def test_a_measurement_rebuilt_after_the_leg_does_not_vouch_for_the_content(tmp_path):
    _touch(tmp_path / "a.py", LEG + timedelta(hours=1))
    _record(tmp_path, rev_at=LEG + timedelta(minutes=30))
    reason = stale_tests_refusal(tmp_path, ["a.py"], {}, RUN)
    assert reason is not None and reason.startswith(STALE_TESTS_REASON)


def test_no_measurement_record_leaves_a_newer_write_refused(tmp_path):
    _touch(tmp_path / "a.py", LEG + timedelta(hours=1))
    _record(tmp_path, rev=None)
    assert stale_tests_refusal(tmp_path, ["a.py"], {}, RUN) is not None


def test_a_directory_write_peer_touch_is_not_an_edit_but_a_changed_file_is(tmp_path):
    _touch(tmp_path / "d" / "f.py")
    _touch(tmp_path / "d" / "g.py")
    _record(tmp_path, rev=("d/f.py", "d/g.py"))
    _touch(tmp_path / "d" / "f.py", LEG + timedelta(hours=1))
    assert stale_tests_refusal(tmp_path, ["d"], {}, RUN) is None
    (tmp_path / "d" / "g.py").write_text("changed\n", encoding="utf-8")
    _touch(tmp_path / "d" / "g.py", LEG + timedelta(hours=1))
    reason = stale_tests_refusal(tmp_path, ["d"], {}, RUN)
    assert reason is not None and "d/g.py" in reason


def test_a_later_item_stamp_still_passes_over_an_older_leg(tmp_path):
    _touch(tmp_path / "a.py", LEG + timedelta(hours=1))
    _record(tmp_path)
    stamp = (LEG + timedelta(hours=2)).isoformat()
    assert stale_tests_refusal(tmp_path, ["a.py"], _item(stamp), RUN) is None


def test_halt_stamp_reads_the_record_dirs_manifest_for_the_own_diff_mtime_check(tmp_path, monkeypatch):
    from coordinator_core.git import git_state
    from coordinator_core.ops.dispatch_emit import test_leg_freshness as tlf

    head = "a" * 40
    monkeypatch.setattr(git_state, "head_sha", lambda root: head)
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "test-leg.json").write_text(
        json.dumps({"measured_at": LEG.isoformat(), "head_sha": head}), encoding="utf-8"
    )
    (run_dir / "manifest.json").write_text(json.dumps({"own_diff_paths": ["a.py"]}), encoding="utf-8")
    _touch(tmp_path / "a.py", LEG - timedelta(minutes=1))
    assert tlf.halt_stamp(run_dir)["stale"] is False
    _touch(tmp_path / "a.py", LEG + timedelta(minutes=1))
    assert tlf.halt_stamp(run_dir)["stale"] is True
