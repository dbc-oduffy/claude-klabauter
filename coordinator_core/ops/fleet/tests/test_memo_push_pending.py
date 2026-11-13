"""Durable push-pending records round-trip under the sender outbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _memo_push_pending as pp
from coordinator_core.ops.fleet._outbox_enum import outbox_dir

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _record(sha: str = _SHA_A, **over) -> dict:
    row = {
        "receiver_root": "recv",
        "remote": "origin",
        "branch": "work/day",
        "path": "cross-repo/inbox/m.md",
        "blob_sha": "c" * 40,
        "message": "deliver\n",
        "commit_sha": sha,
        "parent_sha": "d" * 40,
    }
    row.update(over)
    return row


def test_round_trip_under_outbox_push_pending(tmp_path: Path) -> None:
    path = pp.write_pending(tmp_path, _record())

    assert path == outbox_dir(tmp_path) / "push-pending" / f"{_SHA_A}.json"
    rows = pp.list_pending(tmp_path)
    assert len(rows) == 1
    assert rows[0]["commit_sha"] == _SHA_A
    assert rows[0]["attempts"] == 0
    assert rows[0]["last_error"] == ""
    assert "record_path" not in json.loads(path.read_text(encoding="utf-8"))
    assert not list(path.parent.glob("*.tmp"))


def test_remove_deletes_and_reports_absence(tmp_path: Path) -> None:
    pp.write_pending(tmp_path, _record())

    assert pp.remove_pending(tmp_path, _SHA_A) is True
    assert pp.list_pending(tmp_path) == []
    assert pp.remove_pending(tmp_path, _SHA_A) is False


def test_list_on_missing_dir_is_empty(tmp_path: Path) -> None:
    assert pp.list_pending(tmp_path) == []


def test_bump_attempt_counts_and_keeps_record(tmp_path: Path) -> None:
    pp.write_pending(tmp_path, _record())

    first = pp.bump_attempt(pp.list_pending(tmp_path)[0], "rejected")
    second = pp.bump_attempt(pp.list_pending(tmp_path)[0], "unreachable")

    assert first["attempts"] == 1
    assert second["attempts"] == 2
    stored = pp.list_pending(tmp_path)[0]
    assert stored["attempts"] == 2
    assert stored["last_error"] == "unreachable"
    assert stored["commit_sha"] == _SHA_A


def test_bump_after_removal_does_not_resurrect(tmp_path: Path) -> None:
    pp.write_pending(tmp_path, _record())
    stale = pp.list_pending(tmp_path)[0]
    pp.remove_pending(tmp_path, _SHA_A)

    assert pp.bump_attempt(stale, "late") is None
    assert pp.list_pending(tmp_path) == []


def test_retarget_writes_new_and_removes_old(tmp_path: Path) -> None:
    pp.write_pending(tmp_path, _record(_SHA_A))

    pp.retarget_pending(tmp_path, _SHA_A, _record(_SHA_B, parent_sha="e" * 40))

    rows = pp.list_pending(tmp_path)
    assert [r["commit_sha"] for r in rows] == [_SHA_B]
    assert rows[0]["parent_sha"] == "e" * 40


def test_malformed_files_are_skipped(tmp_path: Path) -> None:
    pp.write_pending(tmp_path, _record())
    root = pp.pending_dir(tmp_path)
    (root / "junk.json").write_text("{not json", encoding="utf-8")
    (root / "partial.json").write_text('{"commit_sha": "x"}', encoding="utf-8")

    assert [r["commit_sha"] for r in pp.list_pending(tmp_path)] == [_SHA_A]


def test_missing_key_and_bad_sha_are_refused(tmp_path: Path) -> None:
    row = _record()
    del row["blob_sha"]
    with pytest.raises(ValueError, match="blob_sha"):
        pp.write_pending(tmp_path, row)
    with pytest.raises(ValueError, match="hex object id"):
        pp.write_pending(tmp_path, _record("../../evil"))
    assert pp.list_pending(tmp_path) == []
