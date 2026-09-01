"""Tests for `coordinator_core.group_em.watch_heartbeat` -- the standing
watch's presence stamp, and the shape of it that a repo we do not own reads.

THE SHAPE IS PINNED AGAINST A NAMED READER, not against our own writer. The
consumer is `coordinator/skills/group-em/watch_heartbeat.read_watch` on the
DoE plane (feeding `GROUP EM WATCH: <verdict>` on that repo's SessionStart
presence hook). A key we stop writing, or a timestamp format we drift, is
invisible here and shows up over there as a fleet reporting no watcher -- the
exact false-negative the module exists to prevent. So `_READER_KEYS` and the
timestamp format below are transcribed from that reader deliberately: if this
test has to change, a cross-repo memo goes with it.
"""

from __future__ import annotations

import calendar
import json
import time

from coordinator_core.group_em import watch_heartbeat


#: Every key `read_watch` reads off the record, plus the two `stamp` writes
#: for a reader's benefit (`tick_source`, `subscribed_peers`).
_READER_KEYS = {
    "holder_session_id",
    "holder_name",
    "last_tick_at",
    "tick_source",
    "next_expected_by",
    "subscribed_peers",
    "declinations",
}

_READER_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _record(tmp_path):
    with open(watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        return json.load(fh)


def test_stamp_writes_exactly_the_keys_the_doe_reader_reads(tmp_path):
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-1", declinations=[], interval_seconds=5.0
    )
    assert set(_record(tmp_path)) == _READER_KEYS


def test_timestamps_parse_in_the_readers_own_format(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-1", declinations=[], interval_seconds=5.0
    )
    record = _record(tmp_path)
    for field in ("last_tick_at", "next_expected_by"):
        # `calendar.timegm(time.strptime(...))` is literally what the reader
        # does; anything it cannot parse degrades that side to `stale`.
        calendar.timegm(time.strptime(record[field], _READER_TIMESTAMP_FORMAT))


def test_tick_source_is_the_readers_reserved_monitor_word(tmp_path):
    """`monitor` is one of the three the DoE writer already declares
    (`cron` | `monitor` | `entry`) -- a `Monitor`-held watch is legible to the
    reader with no change on its side."""
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-1", declinations=[], interval_seconds=5.0
    )
    assert _record(tmp_path)["tick_source"] == "monitor"


def test_holder_is_the_crown_and_the_name_is_never_stored(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-1", declinations=[], interval_seconds=5.0
    )
    record = _record(tmp_path)
    assert record["holder_session_id"] == "crown-1"
    assert record["holder_name"] is None


def test_next_expected_by_is_derived_from_the_interval_not_a_fixed_window(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="crown-1",
        declinations=[],
        interval_seconds=300.0,
        now_epoch=1_000_000.0,
    )
    deadline = calendar.timegm(
        time.strptime(_record(tmp_path)["next_expected_by"], _READER_TIMESTAMP_FORMAT)
    )
    assert deadline == 1_000_000 + 900  # three ticks of a 300s interval


def test_a_fast_interval_still_gets_the_grace_floor(tmp_path):
    """Three ticks of a 5s poll is 15s: shorter than one slow moment under
    fleet load, and the record would flicker `stale` for no reason."""
    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="crown-1",
        declinations=[],
        interval_seconds=5.0,
        now_epoch=1_000_000.0,
    )
    deadline = calendar.timegm(
        time.strptime(_record(tmp_path)["next_expected_by"], _READER_TIMESTAMP_FORMAT)
    )
    assert deadline == 1_000_000 + 60


def test_each_stamp_replaces_the_whole_record_never_accumulates(tmp_path):
    """`declinations` is THIS tick's rows: an accumulating list is what makes
    "looked, nothing to do" indistinguishable from "did not look"."""
    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="crown-1",
        declinations=[{"session_id": "p1", "name": None, "gate": "cooldown", "reason": "r"}],
        interval_seconds=5.0,
    )
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-1", declinations=[], interval_seconds=5.0
    )
    assert _record(tmp_path)["declinations"] == []


def test_stamp_returns_false_rather_than_raising_when_the_path_is_unusable(tmp_path):
    """A missed tick must never end a watch that is otherwise working."""
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-1", declinations=[], interval_seconds=5.0
    ) is False


def test_tick_source_is_the_callers_word_when_a_wake_fired_the_tick(tmp_path):
    """A cron-floor wake and a held poller write the same keys and mean
    different things about what happens if nobody fires again."""
    watch_heartbeat.stamp(str(tmp_path), holder_session_id="crown-1", declinations=[], interval_seconds=1380.0, tick_source="cron")
    assert _record(tmp_path)["tick_source"] == "cron"


def test_an_unknown_tick_source_raises_rather_than_writing_it(tmp_path):
    """The one argument a caller can get wrong silently. An unknown word reads
    to the DoE reader as a watch of unknown provenance -- worse than a loud
    failure at the writer's first run."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        watch_heartbeat.stamp(str(tmp_path), holder_session_id="c", declinations=[], interval_seconds=5.0, tick_source="poller")
