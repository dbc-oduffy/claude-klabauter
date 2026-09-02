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
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    )
    assert set(_record(tmp_path)) == _READER_KEYS


def test_timestamps_parse_in_the_readers_own_format(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
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
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    )
    assert _record(tmp_path)["tick_source"] == "monitor"


def test_holder_is_the_group_em_and_the_name_is_never_stored(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
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
        writer_session_id="w1",
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
        writer_session_id="w1",
    )
    deadline = calendar.timegm(
        time.strptime(_record(tmp_path)["next_expected_by"], _READER_TIMESTAMP_FORMAT)
    )
    assert deadline == 1_000_000 + 60


def test_each_stamp_replaces_the_whole_record_never_accumulates(tmp_path):
    """`declinations` is THIS tick's rows: an accumulating list is what makes
    "looked, nothing to do" indistinguishable from "did not look". Both
    stamps share the same writer/holder/tick_source so the second is not a
    fresh-and-foreign decline."""
    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="group-em-1",
        declinations=[{"session_id": "p1", "name": None, "gate": "cooldown", "reason": "r"}],
        interval_seconds=5.0,
        now_epoch=1_000_000.0,
        writer_session_id="w1",
    )
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        now_epoch=1_000_000.0, writer_session_id="w1",
    )
    assert _record(tmp_path)["declinations"] == []


def test_stamp_returns_false_rather_than_raising_when_the_path_is_unusable(tmp_path):
    """A missed tick must never end a watch that is otherwise working."""
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    ) is False


def test_tick_source_is_the_callers_word_when_a_wake_fired_the_tick(tmp_path):
    """A cron-floor wake and a held poller write the same keys and mean
    different things about what happens if nobody fires again."""
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=1380.0, tick_source="cron", writer_session_id="w1",
    )
    assert _record(tmp_path)["tick_source"] == "cron"


def test_an_unknown_tick_source_raises_rather_than_writing_it(tmp_path):
    """The one argument a caller can get wrong silently. An unknown word reads
    to the DoE reader as a watch of unknown provenance -- worse than a loud
    failure at the writer's first run."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        watch_heartbeat.stamp(
            str(tmp_path), holder_session_id="c", declinations=[], interval_seconds=5.0,
            tick_source="poller", writer_session_id="w1",
        )


def test_an_omitted_writer_session_id_raises_rather_than_writing_unattributed(tmp_path):
    """`writer_session_id` became required 2026-09-01: an omitting call used
    to write `writer_session_id: null`, which is how a crown read its own
    write back as independent confirmation. Retired deliberately -- see the
    pin this replaces two tests below."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        watch_heartbeat.stamp(
            str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0
        )


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
        str(ghost), holder_session_id="s", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    ) is False
    assert not ghost.exists()


def test_the_state_leaf_under_a_real_root_is_still_created(tmp_path):
    """The refusal is one level deep, not a demand that the caller pre-make
    `state/` -- a first tick in a real repo must still write."""
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="s", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
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


# RETIRED 2026-09-01, deliberately, not a silent deletion. This test used to
# assert `record["writer_session_id"] is None` for an omitting call --
# `writer_session_id` was Optional and defaulted to None. It is now required
# (see `test_an_omitted_writer_session_id_raises_rather_than_writing_unattributed`
# above): an omitting call is how a crown read its own write back as
# independent confirmation, so the field now populates on every write instead
# of ever being null. Do not restore the old pin as a "regression" -- the
# opposite behaviour is the fix.
def test_every_successful_write_populates_writer_session_id(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    )
    assert _record(tmp_path)["writer_session_id"] == "w1"


# THREE STATES, THREE ANSWERS. The defect these pin: a watch alive and quiet, a
# watch that died or never started, and a repo nobody ever armed all rendered
# identically to the only surface a human had (`idle`). A renderer that lets any
# two of them collapse again is the bug, so each is asserted against the others.


def _armed(tmp_path, now, subscribed_peers=1, declinations=None):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=list(declinations or []),
        interval_seconds=30.0, holder_name="claude-klabauter-ad", now_epoch=now,
        writer_session_id="w1", subscribed_peers=subscribed_peers,
    )
    return watch_heartbeat.read_liveness(str(tmp_path), now_epoch=now + 3.0)


def test_a_quiet_live_watch_reads_alive_not_idle(tmp_path):
    text = watch_heartbeat.human_verdict(_armed(tmp_path, time.time()))
    assert text.startswith("ALIVE")
    assert "claude-klabauter-ad" in text
    # The quiet itself has to be named, or a reader re-reads silence as a fault.
    assert "Quiet is the normal state" in text


def test_a_watch_past_its_own_deadline_reads_not_running_with_the_restart(tmp_path):
    now = time.time()
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, now_epoch=now - 3600, writer_session_id="w1",
    )
    liveness = watch_heartbeat.read_liveness(str(tmp_path), now_epoch=now)
    assert liveness["verdict"] == watch_heartbeat.VERDICT_STALE
    text = watch_heartbeat.human_verdict(liveness, now_epoch=now)
    assert text.startswith("NOT RUNNING")
    assert watch_heartbeat.REARM_COMMAND in text


def test_a_repo_no_watch_ever_covered_reads_unknown_never_green(tmp_path):
    liveness = watch_heartbeat.read_liveness(str(tmp_path))
    assert liveness["verdict"] == watch_heartbeat.VERDICT_ABSENT
    assert liveness["absent_reason"] == watch_heartbeat.ABSENT_NEVER_ARMED
    text = watch_heartbeat.human_verdict(liveness)
    assert text.startswith("UNKNOWN")
    assert "NOT an all-clear" in text
    assert "ALIVE" not in text


def test_an_unreadable_record_says_so_rather_than_never_armed(tmp_path):
    path = watch_heartbeat.watch_path(str(tmp_path))
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    liveness = watch_heartbeat.read_liveness(str(tmp_path))
    # The verdict stays the sibling reader's single word; only the detail splits.
    assert liveness["verdict"] == watch_heartbeat.VERDICT_ABSENT
    assert liveness["absent_reason"] == watch_heartbeat.ABSENT_UNREADABLE
    assert "cannot be read" in watch_heartbeat.human_verdict(liveness)


def test_the_age_is_read_off_the_z_stamp_as_utc_not_the_local_clock(tmp_path):
    # A `Z` stamp measured against a local clock invents an hour of staleness on
    # any box that is not UTC. Both sides here are epoch seconds; a three-second
    # tick must never render as an hour.
    now = float(int(time.time()))
    text = watch_heartbeat.human_verdict(_armed(tmp_path, now), now_epoch=now + 3.0)
    assert "3 seconds ago" in text
    assert "hours" not in text


# C1 -- PRIOR-HOLDER TRACE AND FRESH-AND-FOREIGN DECLINE. The falsifier's
# exact leg-2 sequence: two crown instances stamping in sequence against a
# throwaway repo_root, with a trace of the first holder surviving in the
# second's record. Distinct holder AND writer ids on each side, per the
# falsifier's own baseline.


def test_the_falsifiers_two_crown_sequence_carries_the_prior_holder(tmp_path):
    ok1 = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-A", declinations=[],
        interval_seconds=30.0, now_epoch=1_000_000.0,
        writer_session_id="crown-A-11111111",
    )
    ok2 = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-B", declinations=[],
        interval_seconds=30.0, now_epoch=1_000_100.0,
        writer_session_id="crown-B-22222222",
    )
    # A decline here would fail the falsifier by construction (leg 2 opens
    # with `if not (ok1 and ok2): return False`).
    assert ok1 is True
    assert ok2 is True
    record = _record(tmp_path)
    assert record["prior_holder_session_id"] == "crown-A"
    assert record["prior_last_tick_at"] is not None


def test_same_holder_same_writer_different_tick_source_still_writes_the_trace(tmp_path):
    """The measured 19:31/19:32 case, at its REAL cadences.

    Cron and monitor share holder AND writer, so a holder-keyed discriminator
    writes no trace here at all -- `tick_source` is a first-class arm of the
    trace's disjunction, not a parenthetical on holder.

    THE INTERVALS ARE THE POINT AND MUST NOT BE SHRUNK. A cron audit tick
    declares `interval_seconds=23*60`, so its record stays FRESH for ~69
    minutes; the monitor follows 50 seconds later on an ~80s cadence. An
    earlier version of this test used `interval_seconds=5.0` and a 100s gap,
    which made the first record STALE and so never exercised the decline at
    all -- it passed while the real cadences deadlocked the watch for 68
    minutes. If this test is ever "simplified" back to short intervals it
    stops testing anything.
    """
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=23 * 60.0, now_epoch=1_000_000.0,
        writer_session_id="w1", tick_source="cron",
    )
    accepted = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=80.0, now_epoch=1_000_050.0,
        writer_session_id="w1", tick_source="monitor",
    )
    assert accepted is True
    record = _record(tmp_path)
    assert record["tick_source"] == "monitor"
    assert record["prior_tick_source"] == "cron"
    assert record["prior_holder_session_id"] == "group-em-1"


def test_a_same_crown_monitor_is_never_locked_out_by_its_own_cron_tick(tmp_path):
    """Regression pin for the deadlock a three-field foreignness caused.

    `is_fresh_and_foreign` is holder-or-writer, deliberately NOT `tick_source`.
    When it also compared `tick_source`, every monitor poll inside a cron
    record's ~69-minute freshness window was declined -- the standing watch
    stopped stamping for over an hour after each audit tick and the record it
    could not refresh read STALE to the whole fleet, which is worse than the
    clobber the trace exists to make visible.

    Ten consecutive monitor polls at the real ~80s cadence, all inside the
    cron record's window, must every one of them land.
    """
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=23 * 60.0, now_epoch=1_000_000.0,
        writer_session_id="w1", tick_source="cron",
    )
    for tick in range(1, 11):
        accepted = watch_heartbeat.stamp(
            str(tmp_path), holder_session_id="group-em-1", declinations=[],
            interval_seconds=80.0, now_epoch=1_000_000.0 + 80.0 * tick,
            writer_session_id="w1", tick_source="monitor",
        )
        assert accepted is True, f"monitor poll {tick} was declined by its own crown's cron record"


def test_a_first_stamp_carries_no_prior_keys_at_all(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, writer_session_id="w1",
    )
    record = _record(tmp_path)
    assert "prior_holder_session_id" not in record
    assert "prior_holder_name" not in record
    assert "prior_tick_source" not in record
    assert "prior_last_tick_at" not in record


def test_a_fresh_foreign_record_is_declined_and_survives_unchanged(tmp_path):
    """The measured cron-at-18:30 / monitor-at-18:42 case: cron's own
    `next_expected_by` is still ahead when the foreign writer arrives."""
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=["cron-row"],
        interval_seconds=1380.0, now_epoch=1_000_000.0,
        writer_session_id="cron-writer", tick_source="cron",
    )
    before = _record(tmp_path)
    declined = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=["monitor-row"],
        interval_seconds=30.0, now_epoch=1_000_020.0,
        writer_session_id="monitor-writer", tick_source="monitor",
    )
    assert declined is False
    assert _record(tmp_path) == before


def test_a_stale_foreign_record_is_not_declined(tmp_path):
    """The previous writer is gone; declining here would deadlock the watch."""
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=["cron-row"],
        interval_seconds=30.0, now_epoch=1_000_000.0,
        writer_session_id="cron-writer", tick_source="cron",
    )
    accepted = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=["monitor-row"],
        interval_seconds=30.0, now_epoch=1_000_200.0,
        writer_session_id="monitor-writer", tick_source="monitor",
    )
    assert accepted is True
    assert _record(tmp_path)["declinations"] == ["monitor-row"]


# ARMED-BANNER SUPPRESSION -- folded in from retired C2. `human_verdict`'s
# ARMED branch used to render the reassurance line unconditionally, over a
# record that could have been clobbered to zero population.


def test_armed_with_zero_population_suppresses_the_reassurance_and_names_the_zero(tmp_path):
    text = watch_heartbeat.human_verdict(
        _armed(tmp_path, time.time(), subscribed_peers=0, declinations=[])
    )
    assert text.startswith("ALIVE")
    assert "Quiet is the normal state" not in text
    assert "0 subscribed peers" in text
    assert "0 declinations" in text


def test_armed_with_real_population_still_renders_the_reassurance(tmp_path):
    text = watch_heartbeat.human_verdict(
        _armed(tmp_path, time.time(), subscribed_peers=3, declinations=[])
    )
    assert text.startswith("ALIVE")
    assert "Quiet is the normal state" in text


# DEFECT 1 -- THE TRACE CARRIES WHAT A TICK COUNTED, NOT ONLY WHO WROTE IT.
# `prior_subscribed_peers` and `prior_declination_count` ride alongside the
# existing identity `prior_*` keys, on the same trigger (`_writer_identity`
# disjunction), and must degrade to `None` rather than crash against an
# older-format prior record that never wrote them.


def test_the_trace_carries_what_the_destroyed_tick_counted(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-A",
        declinations=[{"session_id": "p1", "name": None, "gate": "cooldown", "reason": "r"},
                      {"session_id": "p2", "name": None, "gate": "cooldown", "reason": "r"}],
        interval_seconds=30.0, now_epoch=1_000_000.0,
        subscribed_peers=7, writer_session_id="crown-A-11111111",
    )
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-B", declinations=[],
        interval_seconds=30.0, now_epoch=1_000_100.0,
        writer_session_id="crown-B-22222222",
    )
    record = _record(tmp_path)
    assert record["prior_subscribed_peers"] == 7
    assert record["prior_declination_count"] == 2


def test_an_older_format_prior_record_without_the_new_scalars_does_not_crash(tmp_path):
    """A prior record written before this fix has no `subscribed_peers`
    concept the trace can name -- `.get` returns `None`, not a KeyError, and
    the trace degrades to "unknown" instead of inventing a count."""
    path = watch_heartbeat.watch_path(str(tmp_path))
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "holder_session_id": "crown-A",
            "holder_name": None,
            "last_tick_at": "1970-01-23T03:33:16Z",
            "tick_source": "cron",
            "next_expected_by": "1970-01-23T03:34:16Z",
            "writer_session_id": "crown-A-11111111",
            # no `subscribed_peers` and no `declinations` keys -- both absent,
            # the pre-fix shape.
        }, fh)

    accepted = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-B", declinations=[],
        interval_seconds=30.0, now_epoch=2_000_000.0,
        writer_session_id="crown-B-22222222",
    )
    assert accepted is True
    record = _record(tmp_path)
    assert record["prior_subscribed_peers"] is None
    assert record["prior_declination_count"] is None


def test_stamp_accepts_a_writer_that_differs_from_the_holder_by_design(tmp_path):
    """Review: coordinator:code-reviewer (a2a408f1eb356878e) Finding 3 -- pins
    a deliberate boundary, not an oversight.

    `stamp` validates only that `writer_session_id` is non-empty; it does NOT
    check that the writer agrees with `holder_session_id` or anything else.
    The writer/caller identity-agreement guard closed in `360194cdfb` lives
    entirely at the op layer (`coordinator_core/ops/group_em_stamp.py`,
    pinned in `coordinator_core/ops/tests/test_group_em_crown_instrument_ops.py`),
    confirmed by the sibling reviewer covering `coordinator_core/ops/`.

    EM decision: do not add the guard here. The op is the untrusted surface
    -- an arbitrary JSON-RPC caller -- while `stamp`'s in-process callers are
    the crown's own code passing its own ids. Pushing the check into `stamp`
    would make the standing tick re-verify its own identity on every
    heartbeat, on a hot path under a hard sub-500ms budget, against a caller
    already inside the trust boundary. A same-crown monitor legitimately
    writes with a differing `tick_source`/writer, and the extended trace's
    three-way disjunction depends on exactly this permissiveness -- a guard
    here would break the trace, not harden it. A future reader finding this
    permissiveness should not "fix" it in this function.
    """
    accepted = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="crown-A", declinations=[],
        interval_seconds=5.0, writer_session_id="crown-B-differs-entirely",
    )
    assert accepted is True
    record = _record(tmp_path)
    assert record["holder_session_id"] == "crown-A"
    assert record["writer_session_id"] == "crown-B-differs-entirely"
