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


#: Every key `read_watch` reads off the record, plus the writes `stamp` makes
#: for a reader's benefit (`tick_source`, `subscribed_peers`,
#: `writer_session_id`).
#:
#: `writer_session_id` was added 2026-09-01 and is deliberately inside this pin
#: rather than exempted from it. The sibling reader takes the keys it wants BY
#: NAME (`record.get(...)`, verified against its own source), so an added key
#: cannot break it -- but the pin's job is to make any change to this record's
#: shape a decision someone writes down, and quietly loosening it to "at least
#: these" would retire that job while looking like a smaller edit than removing
#: the test.
_READER_KEYS = {
    "holder_session_id",
    "holder_name",
    "last_tick_at",
    "tick_source",
    "next_expected_by",
    "subscribed_peers",
    "declinations",
    "writer_session_id",
}

_READER_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _record(tmp_path):
    with open(watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        return json.load(fh)


def test_stamp_writes_exactly_the_keys_the_doe_reader_reads(tmp_path):
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
    )
    assert set(_record(tmp_path)) == _READER_KEYS


def test_timestamps_parse_in_the_readers_own_format(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
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
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
    )
    assert _record(tmp_path)["tick_source"] == "monitor"


def test_holder_is_the_group_em_and_the_name_is_never_stored(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
    )
    record = _record(tmp_path)
    assert record["holder_session_id"] == "group-em-1"
    assert record["holder_name"] is None


def test_next_expected_by_is_derived_from_the_interval_not_a_fixed_window(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="group-em-1",
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
        holder_session_id="group-em-1",
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
        holder_session_id="group-em-1",
        declinations=[{"session_id": "p1", "name": None, "gate": "cooldown", "reason": "r"}],
        interval_seconds=5.0,
    )
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
    )
    assert _record(tmp_path)["declinations"] == []


def test_stamp_returns_false_rather_than_raising_when_the_path_is_unusable(tmp_path):
    """A missed tick must never end a watch that is otherwise working."""
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
    ) is False


def test_tick_source_is_the_callers_word_when_a_wake_fired_the_tick(tmp_path):
    """A cron-floor wake and a held poller write the same keys and mean
    different things about what happens if nobody fires again."""
    watch_heartbeat.stamp(str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=1380.0, tick_source="cron")
    assert _record(tmp_path)["tick_source"] == "cron"


def test_an_unknown_tick_source_raises_rather_than_writing_it(tmp_path):
    """The one argument a caller can get wrong silently. An unknown word reads
    to the DoE reader as a watch of unknown provenance -- worse than a loud
    failure at the writer's first run."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        watch_heartbeat.stamp(str(tmp_path), holder_session_id="c", declinations=[], interval_seconds=5.0, tick_source="poller")


def test_the_writer_refuses_to_mint_a_repo_it_was_pointed_at(tmp_path):
    """A heartbeat writer that can conjure a repo directory is doing something
    no correct caller ever needs.

    Measured 2026-09-01: a drive-relative `X:example-game-workbench-repo` (a
    backslash path that lost its separators to a shell) resolved against the
    writer's cwd, and this function created the whole chain there -- a
    repo-shaped tree inside a publish mirror, where it failed a publish row's
    content check and blocked the round for the fleet. The `state/` leaf under
    an EXISTING root is ours to create; the root is not.
    """
    ghost = tmp_path / "never-existed"
    assert watch_heartbeat.stamp(
        str(ghost), holder_session_id="s", declinations=[], interval_seconds=5.0
    ) is False
    assert not ghost.exists()


def test_the_state_leaf_under_a_real_root_is_still_created(tmp_path):
    """The refusal is one level deep, not a demand that the caller pre-make
    `state/` -- a first tick in a real repo must still write."""
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="s", declinations=[], interval_seconds=5.0
    ) is True
    assert (tmp_path / "state" / "group-em-watch.json").is_file()


def test_the_record_says_which_process_wrote_it_not_only_who_holds_it(tmp_path):
    """`holder_session_id` is the Group-EM in every case, including ticks a
    dispatched teammate writes on its behalf -- so it cannot answer "did I
    write this?". A fleet-watch read back a `subscribed_peers` value its own
    Group-EM had written minutes earlier and reported it to that Group-EM as
    independent confirmation (2026-09-01). Whole-file replace plus no writer
    attribution is what makes an echo indistinguishable from a confirmation.
    """
    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="group-em-1",
        declinations=[],
        interval_seconds=5.0,
        writer_session_id="teammate-9",
    )
    record = _record(tmp_path)
    assert record["holder_session_id"] == "group-em-1"
    assert record["writer_session_id"] == "teammate-9"


def test_an_unattributed_write_says_so_rather_than_claiming_the_holder(tmp_path):
    """A writer that does not name itself must leave the field null, never
    default to the holder -- that would manufacture exactly the false
    attribution the field exists to prevent."""
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
    )
    assert _record(tmp_path)["writer_session_id"] is None
