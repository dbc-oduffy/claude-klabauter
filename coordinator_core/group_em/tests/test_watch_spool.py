"""Tests for `coordinator_core.group_em.watch_spool` -- the wake spool's
reader, debounce, and race-safe compaction (sizing
`state/sizings/2026-09-01-the-group-em-wake-gets-the-spool-it-is-m.yaml`).

Covers: malformed/torn lines skipped not fatal, unknown keys tolerated, an
absent spool is not an error, the debounce suppresses only when the spool has
nothing newer than the heartbeat, and compaction under a concurrent-append
race.
"""

from __future__ import annotations

import json
import os
import time

from coordinator_core.group_em import watch_heartbeat
from coordinator_core.group_em import watch_spool


def _write_lines(repo_root, lines):
    os.makedirs(os.path.join(str(repo_root), "state"), exist_ok=True)
    with open(watch_spool.spool_path(str(repo_root)), "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line)
            fh.write("\n")


def _stamp_heartbeat(repo_root, at_epoch):
    (repo_root / "state").mkdir(exist_ok=True)
    watch_heartbeat.stamp(
        str(repo_root),
        holder_session_id="gem-1",
        declinations=[],
        interval_seconds=60.0,
        now_epoch=at_epoch,
        writer_session_id="gem-1",
    )


def test_absent_spool_is_not_an_error(tmp_path):
    (tmp_path / "state").mkdir()
    assert list(watch_spool.read_records(str(tmp_path))) == []
    assert watch_spool.newest_at_epoch(str(tmp_path)) is None
    # `compact` on an absent spool has nothing to do and reports success.
    assert watch_spool.compact(str(tmp_path), drain_point_epoch=time.time()) is True


def test_malformed_and_torn_lines_are_skipped_not_fatal(tmp_path):
    good = json.dumps(
        {"session_id": "s1", "state": "PAUSED:idle", "at": "2026-09-02T10:00:00Z",
         "writer": "receiver-state-sensor"}
    )
    torn = '{"session_id": "s2", "state": "PAUSED:i'  # interleaved/truncated write
    scalar = "42"  # valid JSON, not an object
    _write_lines(tmp_path, [good, torn, scalar, "", "   "])

    records = list(watch_spool.read_records(str(tmp_path)))
    assert len(records) == 1
    assert records[0]["session_id"] == "s1"


def test_unknown_keys_are_tolerated(tmp_path):
    line = json.dumps(
        {"session_id": "s1", "state": "PAUSED:idle", "at": "2026-09-02T10:00:00Z",
         "writer": "receiver-state-sensor", "future_field": "whatever"}
    )
    _write_lines(tmp_path, [line])
    records = list(watch_spool.read_records(str(tmp_path)))
    assert records[0]["future_field"] == "whatever"


def test_debounce_suppresses_when_heartbeat_is_fresher_than_every_record(tmp_path):
    base = 1_800_000_000.0
    _stamp_heartbeat(tmp_path, base)
    _write_lines(
        tmp_path,
        [
            json.dumps(
                {"session_id": "s1", "state": "PAUSED:idle",
                 "at": watch_heartbeat.iso_instant(base - 120), "writer": "x"}
            )
        ],
    )
    assert watch_spool.should_suppress_wake(str(tmp_path)) is True


def test_debounce_does_not_suppress_when_a_record_is_newer(tmp_path):
    base = 1_800_000_000.0
    _stamp_heartbeat(tmp_path, base)
    _write_lines(
        tmp_path,
        [
            json.dumps(
                {"session_id": "s1", "state": "PAUSED:idle",
                 "at": watch_heartbeat.iso_instant(base + 120), "writer": "x"}
            )
        ],
    )
    assert watch_spool.should_suppress_wake(str(tmp_path)) is False


def test_debounce_never_suppresses_before_any_heartbeat_exists(tmp_path):
    (tmp_path / "state").mkdir()
    _write_lines(
        tmp_path,
        [json.dumps({"session_id": "s1", "state": "PAUSED:idle",
                     "at": "2026-09-02T10:00:00Z", "writer": "x"})],
    )
    assert watch_spool.should_suppress_wake(str(tmp_path)) is False


def test_debounce_never_suppresses_when_the_spool_file_itself_is_absent(tmp_path):
    base = 1_800_000_000.0
    _stamp_heartbeat(tmp_path, base)
    assert not os.path.exists(watch_spool.spool_path(str(tmp_path)))
    assert watch_spool.should_suppress_wake(str(tmp_path)) is False


def test_debounce_suppresses_on_an_existing_but_empty_spool(tmp_path):
    base = 1_800_000_000.0
    _stamp_heartbeat(tmp_path, base)
    _write_lines(tmp_path, [])
    assert os.path.exists(watch_spool.spool_path(str(tmp_path)))
    assert watch_spool.should_suppress_wake(str(tmp_path)) is True


def test_compaction_drops_records_at_or_older_than_the_drain_point(tmp_path):
    base = 1_800_000_000.0
    old = json.dumps({"session_id": "s1", "state": "PAUSED:idle",
                       "at": watch_heartbeat.iso_instant(base - 10), "writer": "x"})
    at_point = json.dumps({"session_id": "s2", "state": "PAUSED:idle",
                            "at": watch_heartbeat.iso_instant(base), "writer": "x"})
    newer = json.dumps({"session_id": "s3", "state": "PAUSED:idle",
                         "at": watch_heartbeat.iso_instant(base + 10), "writer": "x"})
    _write_lines(tmp_path, [old, at_point, newer])

    assert watch_spool.compact(str(tmp_path), drain_point_epoch=base) is True

    remaining = list(watch_spool.read_records(str(tmp_path)))
    assert [r["session_id"] for r in remaining] == ["s3"]


def test_compaction_preserves_a_concurrent_append_after_the_read_snapshot(tmp_path, monkeypatch):
    base = 1_800_000_000.0
    old = json.dumps({"session_id": "s1", "state": "PAUSED:idle",
                       "at": watch_heartbeat.iso_instant(base - 10), "writer": "x"})
    _write_lines(tmp_path, [old])

    concurrent = json.dumps({"session_id": "s2", "state": "PAUSED:idle",
                              "at": watch_heartbeat.iso_instant(base + 999), "writer": "peer"})

    real_open = open
    state = {"appended": False}

    def _open_with_race(path, mode="r", *args, **kwargs):
        # The FIRST read inside `compact` is the snapshot read (`"rb"`); the
        # instant it happens, simulate a producer appending a new record --
        # BEFORE `compact` re-opens the file to capture the post-snapshot tail.
        handle = real_open(path, mode, *args, **kwargs)
        if (
            not state["appended"]
            and mode == "rb"
            and str(path) == watch_spool.spool_path(str(tmp_path))
        ):
            state["appended"] = True
            with real_open(path, "a", encoding="utf-8") as append_fh:
                append_fh.write(concurrent)
                append_fh.write("\n")
        return handle

    monkeypatch.setattr("builtins.open", _open_with_race)

    assert watch_spool.compact(str(tmp_path), drain_point_epoch=base) is True

    remaining = [json.loads(line) for line in
                 open(watch_spool.spool_path(str(tmp_path)), encoding="utf-8").read().splitlines()
                 if line.strip()]
    session_ids = {r["session_id"] for r in remaining}
    # The old record (older than the drain point) is dropped; the
    # concurrently-appended record, invisible to the snapshot compact() read
    # from, survives via the tail-stabilization re-read.
    assert "s1" not in session_ids
    assert "s2" in session_ids


def test_append_creates_the_file_and_noops_when_state_dir_is_missing(tmp_path):
    missing_state_root = tmp_path / "no-state-here"
    missing_state_root.mkdir()
    assert watch_spool.append(str(missing_state_root), "s1", "PAUSED:idle") is False
    assert not os.path.exists(watch_spool.spool_path(str(missing_state_root)))

    (tmp_path / "state").mkdir()
    assert watch_spool.append(str(tmp_path), "s1", "PAUSED:idle") is True
    records = list(watch_spool.read_records(str(tmp_path)))
    assert len(records) == 1
    assert records[0]["session_id"] == "s1"


def test_debounce_never_suppresses_against_a_stale_watch(tmp_path):
    """A dead watch must never debounce the cron floor -- the fix this pins.

    As first built the debounce asked only whether `last_tick_at` existed, so
    a spool holding nothing newer suppressed the wake HOWEVER stale the
    heartbeat was. That silences the floor exactly in the vacant window the
    floor is the belt-and-suspenders for: this repo sat in precisely that
    window on 2026-09-02 (`GROUP EM WATCH: vacant`, holder exited), which is
    the state a cron-only fallback exists to survive. The freshness term is
    `read_liveness`, so only `armed` suppresses.
    """
    stale = time.time() - 86400
    _stamp_heartbeat(tmp_path, stale)
    _write_lines(tmp_path, [json.dumps(
        {"session_id": "p1", "state": "PAUSED:turn-ended",
         "at": watch_heartbeat.iso_instant(stale - 60), "writer": "s"}
    )])
    assert watch_heartbeat.read_liveness(str(tmp_path))["verdict"] == watch_heartbeat.VERDICT_STALE
    # Every OTHER term says suppress -- spool present, nothing newer than the
    # last tick. Only the liveness verdict stands between the floor and silence.
    assert watch_spool.should_suppress_wake(str(tmp_path)) is False
