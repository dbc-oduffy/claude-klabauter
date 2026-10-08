"""Tests coordinator/lib/receiver_state_reader.py's staleness-window computation.

Zero-spawn: every fixture is synthesized JSON in a tmp dir; nothing here reads
the live `.git/coordinator-sessions/` tree."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[2] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import receiver_state_reader as rsr  # noqa: E402


SID = "abc123"


def _write_record(repo_root: str, session_id: str, record: dict) -> str:
    sdir = os.path.join(repo_root, ".git", "coordinator-sessions", session_id)
    os.makedirs(sdir, exist_ok=True)
    path = os.path.join(sdir, "receiver-state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    return path


def _now_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_no_file_is_unavailable(tmp_path):
    result = rsr.read_receiver_state(str(tmp_path), SID)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_NO_FILE
    assert result["stamped_at"] is None


def test_unsafe_session_id_is_unavailable(tmp_path):
    result = rsr.read_receiver_state(str(tmp_path), "../escape")
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_NO_FILE


def test_bare_dotdot_session_id_does_not_escape_sessions_directory(tmp_path):
    # A bare ".." contains no path separator, so a `^[A-Za-z0-9._-]+$` session-id
    # check would admit it (every char is in the allowed class), and
    # `receiver_state_path` would resolve one directory above the intended
    # `.git/coordinator-sessions/<sid>/` sandbox. This test pins the rejection.
    escape_target = os.path.join(str(tmp_path), ".git", "receiver-state.json")
    os.makedirs(os.path.dirname(escape_target), exist_ok=True)
    with open(escape_target, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 1}, fh)

    assert rsr.receiver_state_path(str(tmp_path), "..") is None

    result = rsr.read_receiver_state(str(tmp_path), "..")
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_NO_FILE


def test_single_dot_session_id_is_rejected(tmp_path):
    assert rsr.receiver_state_path(str(tmp_path), ".") is None


def test_malformed_json_is_unavailable(tmp_path):
    sdir = os.path.join(str(tmp_path), ".git", "coordinator-sessions", SID)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "receiver-state.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    result = rsr.read_receiver_state(str(tmp_path), SID)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_MALFORMED


def test_unreadable_directory_in_place_of_file_is_unavailable(tmp_path):
    sdir = os.path.join(str(tmp_path), ".git", "coordinator-sessions", SID)
    os.makedirs(os.path.join(sdir, "receiver-state.json"), exist_ok=True)
    result = rsr.read_receiver_state(str(tmp_path), SID)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_NO_FILE


def test_unreadable_file_raising_oserror_is_unavailable(tmp_path, monkeypatch):
    # Exercises the `except OSError` branch (REASON_UNREADABLE); the
    # "directory in place of file" test hits REASON_NO_FILE instead.
    # Monkeypatch `open` in the module so a real file exists
    # (passes the `os.path.isfile` check) but raises on read, forcing the
    # OSError branch specifically.
    _write_record(
        str(tmp_path),
        SID,
        {"schema_version": 1, "session_id": SID, "verdict": "PAUSED", "reason": "turn-ended", "stamped_at": "2026-08-30T10:32:48Z"},
    )

    def _raise_oserror(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(rsr, "open", _raise_oserror, raising=False)
    result = rsr.read_receiver_state(str(tmp_path), SID)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_UNREADABLE


def test_unrecognised_schema_version_is_unavailable(tmp_path):
    now = datetime(2026, 8, 30, 10, 32, 48, tzinfo=timezone.utc)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 2,
            "session_id": SID,
            "verdict": "PAUSED",
            "reason": "turn-ended",
            "stamped_at": _now_iso(now),
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_BAD_SCHEMA_VERSION


@pytest.mark.parametrize(
    "record",
    [
        {"schema_version": 1, "session_id": SID, "reason": "turn-ended", "stamped_at": "2026-08-30T10:32:48Z"},
        {"schema_version": 1, "session_id": SID, "verdict": "NOT-A-TAG", "reason": "x", "stamped_at": "2026-08-30T10:32:48Z"},
        {"schema_version": 1, "session_id": SID, "verdict": "PAUSED", "stamped_at": "2026-08-30T10:32:48Z"},
        {"schema_version": 1, "session_id": SID, "verdict": "PAUSED", "reason": "turn-ended"},
        {"schema_version": 1, "session_id": SID, "verdict": "PAUSED", "reason": "turn-ended", "stamped_at": "not-a-timestamp"},
    ],
)
def test_missing_or_malformed_required_field_is_unavailable(tmp_path, record):
    _write_record(str(tmp_path), SID, record)
    now = datetime(2026, 8, 30, 10, 32, 48, tzinfo=timezone.utc)
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_MALFORMED


def test_observed_paused_turn_ended_payload_verbatim(tmp_path):
    now = datetime(2026, 8, 30, 10, 32, 48, tzinfo=timezone.utc)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "PAUSED",
            "reason": "turn-ended",
            "stamped_at": _now_iso(now),
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == "PAUSED"
    assert result["reason"] == "turn-ended"
    assert result["unmodelled_type"] is None
    assert result["unmodelled_subtype"] is None


def test_observed_unknown_unmodelled_payload_verbatim(tmp_path):
    now = datetime(2026, 8, 30, 10, 45, 18, tzinfo=timezone.utc)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "UNKNOWN",
            "reason": "unmodelled line type='atis-latch' subtype=''",
            "stamped_at": _now_iso(now),
            "unmodelled_type": "atis-latch",
            "unmodelled_subtype": "",
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == "UNKNOWN"
    assert result["unmodelled_type"] == "atis-latch"
    assert result["unmodelled_subtype"] == ""


def test_observed_unknown_no_substantive_line_payload(tmp_path):
    now = datetime(2026, 8, 30, 10, 40, 0, tzinfo=timezone.utc)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "UNKNOWN",
            "reason": "no substantive line survived the isSidechain filter and control-line walk-back",
            "stamped_at": _now_iso(now),
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == "UNKNOWN"
    assert result["unmodelled_type"] is None


def test_ignores_cpu_cursor_field(tmp_path):
    now = datetime(2026, 8, 30, 10, 32, 48, tzinfo=timezone.utc)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "PRODUCING",
            "reason": "tool-in-flight",
            "stamped_at": _now_iso(now),
            "cpu_cursor": {"cpu_seconds": 12.3, "wall_clock_epoch": 1234567.0},
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert "cpu_cursor" not in result
    assert result["verdict"] == "PRODUCING"


def test_fresh_record_just_inside_staleness_window_is_reported(tmp_path):
    stamp = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
    now = stamp + timedelta(seconds=rsr.STALE_AFTER_SECONDS)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "PAUSED",
            "reason": "turn-ended",
            "stamped_at": _now_iso(stamp),
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == "PAUSED"
    assert result["stamped_at"] == _now_iso(stamp)


def test_record_just_past_staleness_window_is_withdrawn_to_unavailable(tmp_path):
    stamp = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
    now = stamp + timedelta(seconds=rsr.STALE_AFTER_SECONDS + 1)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "PAUSED",
            "reason": "turn-ended",
            "stamped_at": _now_iso(stamp),
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_STALE
    assert result["stamped_at"] == _now_iso(stamp)


def test_stale_never_reports_a_manufactured_ladder_verdict(tmp_path):
    stamp = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
    now = stamp + timedelta(days=15)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "PRODUCING",
            "reason": "mid-turn",
            "stamped_at": _now_iso(stamp),
        },
    )
    result = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    assert result["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert result["reason"] == rsr.REASON_STALE


def test_unavailable_is_distinct_from_ladder_unknown(tmp_path):
    now = datetime(2026, 8, 30, 10, 32, 48, tzinfo=timezone.utc)
    _write_record(
        str(tmp_path),
        SID,
        {
            "schema_version": 1,
            "session_id": SID,
            "verdict": "UNKNOWN",
            "reason": "unmodelled line type='x' subtype=''",
            "stamped_at": _now_iso(now),
        },
    )
    ladder_unknown = rsr.read_receiver_state(str(tmp_path), SID, now=now)
    missing = rsr.read_receiver_state(str(tmp_path), "nonexistent-sid", now=now)
    assert ladder_unknown["verdict"] == "UNKNOWN"
    assert missing["verdict"] == rsr.VERDICT_UNAVAILABLE
    assert ladder_unknown["verdict"] != missing["verdict"]


def test_receiver_state_path_rejects_unsafe_session_id(tmp_path):
    assert rsr.receiver_state_path(str(tmp_path), "../escape") is None
    assert rsr.receiver_state_path(str(tmp_path), "") is None


def test_receiver_state_path_shape(tmp_path):
    path = rsr.receiver_state_path(str(tmp_path), SID)
    assert path == os.path.join(str(tmp_path), ".git", "coordinator-sessions", SID, "receiver-state.json")
