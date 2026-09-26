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
import os
import time

from coordinator_core.group_em import watch_heartbeat


_READER_KEYS = {
    "holder_session_id",
    "holder_name",
    "last_tick_at",
    "tick_source",
    "next_expected_by",
    "subscribed_peers",
    "declinations",
    "writer_session_id",
    "pid",
    "pid_start_epoch",
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
        calendar.timegm(time.strptime(record[field], _READER_TIMESTAMP_FORMAT))


def test_tick_source_is_the_readers_reserved_monitor_word(tmp_path):
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
    assert deadline == 1_000_000 + 900


def test_a_fast_interval_still_gets_the_grace_floor(tmp_path):
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
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    ) is False


def test_tick_source_is_the_callers_word_when_a_wake_fired_the_tick(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=1380.0, tick_source="cron", writer_session_id="w1",
    )
    assert _record(tmp_path)["tick_source"] == "cron"


def test_an_unknown_tick_source_raises_rather_than_writing_it(tmp_path):
    import pytest as _pytest

    with _pytest.raises(ValueError):
        watch_heartbeat.stamp(
            str(tmp_path), holder_session_id="c", declinations=[], interval_seconds=5.0,
            tick_source="poller", writer_session_id="w1",
        )


def test_an_omitted_writer_session_id_raises_rather_than_writing_unattributed(tmp_path):
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
    assert watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="s", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    ) is True
    assert (tmp_path / "state" / "group-em-watch.json").is_file()


def test_the_record_says_which_process_wrote_it_not_only_who_holds_it(tmp_path):
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


def test_every_successful_write_populates_writer_session_id(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[], interval_seconds=5.0,
        writer_session_id="w1",
    )
    assert _record(tmp_path)["writer_session_id"] == "w1"


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
    assert liveness["verdict"] == watch_heartbeat.VERDICT_ABSENT
    assert liveness["absent_reason"] == watch_heartbeat.ABSENT_UNREADABLE
    assert "cannot be read" in watch_heartbeat.human_verdict(liveness)


def test_the_age_is_read_off_the_z_stamp_as_utc_not_the_local_clock(tmp_path):
    now = float(int(time.time()))
    text = watch_heartbeat.human_verdict(_armed(tmp_path, now), now_epoch=now + 3.0)
    assert "3 seconds ago" in text
    assert "hours" not in text


# C1 -- PRIOR-HOLDER TRACE AND FRESH-AND-FOREIGN DECLINE. The falsifier's


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


def test_a_keyless_record_under_the_same_holder_does_not_lock_the_crown_out(tmp_path):
    record = {
        "holder_session_id": "group-em-1",
        "holder_name": None,
        "last_tick_at": "2026-09-02T17:12:05Z",
        "next_expected_by": "2026-09-02T17:35:05Z",
        "subscribed_peers": 0,
        "declinations": [],
        "tick_source": "entry",
    }
    fresh = calendar.timegm(time.strptime("2026-09-02T17:15:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    assert "writer_session_id" not in record
    assert watch_heartbeat.is_fresh_and_foreign(
        record, fresh, "group-em-1", "monitor-writer"
    ) is False
    # A DIFFERENT holder in the same keyless record is still a live peer crown.
    assert watch_heartbeat.is_fresh_and_foreign(
        record, fresh, "group-em-2", "monitor-writer"
    ) is True


def test_a_stale_foreign_record_is_not_declined(tmp_path):
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
    """Pins
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


def test_rearm_command_spells_both_required_flags():
    assert "group-em-watch --repo-root" in watch_heartbeat.REARM_COMMAND
    assert watch_heartbeat.REARM_COMMAND.count("--group-em-session-id") == 2


def test_no_advertised_rearm_instruction_in_these_three_files_omits_the_holder_id():
    import os

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))
    watch_py = os.path.join(repo_root, "coordinator_core", "group_em", "watch.py")
    bin_py = os.path.join(repo_root, "coordinator", "bin", "group-em-watch.py")

    with open(watch_py, "r", encoding="utf-8") as fh:
        watch_src = fh.read()
    with open(bin_py, "r", encoding="utf-8") as fh:
        bin_src = fh.read()

    dispatched_teammate_idx = watch_src.index(
        "When a dispatched teammate holds the watch"
    )
    dispatched_teammate_line = watch_src[
        dispatched_teammate_idx:watch_src.index("\"\"\"", dispatched_teammate_idx)
    ]
    assert "python -m coordinator_core.group_em.watch --repo-root <path>" in (
        dispatched_teammate_line
    )
    assert "--group-em-session-id <the Group-EM's session id>" in dispatched_teammate_line

    assert "group-em-watch --repo-root <root>" in bin_src
    assert "--group-em-session-id <sid>" in bin_src


def test_process_confirmed_alive_true_for_this_very_process(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, writer_session_id="w1",
    )
    liveness = watch_heartbeat.read_liveness(str(tmp_path))
    assert watch_heartbeat.process_confirmed_alive(liveness) is True


def test_process_confirmed_alive_false_for_a_recycled_or_dead_pid():
    liveness = {"pid": 99999, "pid_start_epoch": 1}
    assert watch_heartbeat.process_confirmed_alive(liveness) is False


def test_process_confirmed_alive_none_without_a_pid():
    assert watch_heartbeat.process_confirmed_alive({}) is None
    assert watch_heartbeat.process_confirmed_alive({"pid": None}) is None


def test_process_confirmed_alive_none_with_a_pid_but_no_birth_epoch():
    liveness = {"pid": os.getpid(), "pid_start_epoch": None}
    assert watch_heartbeat.process_confirmed_alive(liveness) is None


def test_human_verdict_names_both_the_holder_and_its_session_id(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="session-a", declinations=[],
        interval_seconds=30.0, holder_name="claude-klabauter-em",
        writer_session_id="w1",
    )
    liveness = watch_heartbeat.read_liveness(str(tmp_path))
    text = watch_heartbeat.human_verdict(liveness)
    assert "claude-klabauter-em" in text
    assert "session-a" in text


def test_human_verdict_disambiguates_two_holders_sharing_one_name(tmp_path):
    """Two live sessions can carry the same display name -- the record's
    `holder_session_id` is what tells them apart, so it must be on the
    line whenever a name is too. Two separate repo roots stand in for two
    separate crowns' records (a single record can only ever name one
    holder at a time) -- what is under test is the RENDERING, not a
    takeover sequence."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()

    watch_heartbeat.stamp(
        str(repo_a), holder_session_id="session-a", declinations=[],
        interval_seconds=30.0, holder_name="claude-klabauter-em",
        writer_session_id="w1",
    )
    watch_heartbeat.stamp(
        str(repo_b), holder_session_id="session-b", declinations=[],
        interval_seconds=30.0, holder_name="claude-klabauter-em",
        writer_session_id="w2",
    )

    text_a = watch_heartbeat.human_verdict(watch_heartbeat.read_liveness(str(repo_a)))
    text_b = watch_heartbeat.human_verdict(watch_heartbeat.read_liveness(str(repo_b)))

    assert text_a != text_b
    assert "session-a" in text_a
    assert "session-b" in text_b


def test_read_liveness_carries_next_expected_by_when_armed(tmp_path):
    now = time.time()
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, now_epoch=now, writer_session_id="w1",
    )
    liveness = watch_heartbeat.read_liveness(str(tmp_path), now_epoch=now + 1)
    assert liveness["verdict"] == watch_heartbeat.VERDICT_ARMED
    assert liveness["next_expected_by"] == watch_heartbeat.next_expected_by(now, 30.0)
    assert liveness["seconds_overdue"] is None


def test_read_liveness_carries_next_expected_by_when_stale(tmp_path):
    now = time.time()
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, now_epoch=now - 3600, writer_session_id="w1",
    )
    liveness = watch_heartbeat.read_liveness(str(tmp_path), now_epoch=now)
    assert liveness["verdict"] == watch_heartbeat.VERDICT_STALE
    assert liveness["next_expected_by"] is not None
    assert isinstance(liveness["seconds_overdue"], (int, float))


def test_declinations_alone_do_not_vouch_for_a_watch_covering_nobody(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1",
        declinations=[{"session_id": "p1", "name": None, "gate": "cooldown", "reason": "r"}],
        interval_seconds=30.0, subscribed_peers=0, tick_source="entry",
        writer_session_id="w1",
    )
    text = watch_heartbeat.human_verdict(watch_heartbeat.read_liveness(str(tmp_path)))
    assert text.startswith("ALIVE")
    assert "Quiet is the normal state" not in text
    assert "0 subscribed peers and 1 declinations this tick" in text


def test_an_entry_tick_says_it_was_stamped_not_that_it_checked_the_fleet(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, subscribed_peers=3, tick_source="entry",
        writer_session_id="w1",
    )
    text = watch_heartbeat.human_verdict(watch_heartbeat.read_liveness(str(tmp_path)))
    assert "checked the fleet" not in text
    assert "was stamped" in text


def test_a_monitor_tick_still_says_it_checked_the_fleet(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, subscribed_peers=3, tick_source="monitor",
        writer_session_id="w1",
    )
    text = watch_heartbeat.human_verdict(watch_heartbeat.read_liveness(str(tmp_path)))
    assert "checked the fleet" in text


# --- C3(b): `next_expected_by` bases the deadline on the MEASURED cadence,


def test_next_expected_by_falls_back_to_declared_on_the_first_tick():
    assert watch_heartbeat.next_expected_by(1_000_000.0, 300.0) == (
        watch_heartbeat.next_expected_by(1_000_000.0, 300.0, None)
    )


def test_next_expected_by_uses_the_observed_delta_when_it_is_tighter():
    """A monitor DECLARING 18s but OBSERVED at ~80s must not stamp a deadline
    3.4x further ahead than the observed cadence actually earns -- the exact
    measured mismatch this row exists to close."""
    deadline_declared_only = calendar.timegm(time.strptime(
        watch_heartbeat.next_expected_by(1_000_000.0, 80.0), _READER_TIMESTAMP_FORMAT
    ))
    deadline_observed = calendar.timegm(time.strptime(
        watch_heartbeat.next_expected_by(1_000_000.0, 18.0, 18.0),
        _READER_TIMESTAMP_FORMAT,
    ))
    # the floor); the observed delta must not exceed what the DECLARED
    assert deadline_observed <= deadline_declared_only


def test_next_expected_by_caps_a_slower_observed_delta_at_the_declared_interval():
    capped = watch_heartbeat.next_expected_by(1_000_000.0, 18.0, 80.0)
    uncapped_declared_only = watch_heartbeat.next_expected_by(1_000_000.0, 18.0)
    assert capped == uncapped_declared_only


def test_stamp_derives_next_expected_by_from_the_measured_inter_tick_delta(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=80.0, now_epoch=1_000_000.0, writer_session_id="w1",
        tick_source="monitor",
    )
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=80.0, now_epoch=1_000_018.0, writer_session_id="w1",
        tick_source="monitor",
    )
    deadline = calendar.timegm(time.strptime(
        _record(tmp_path)["next_expected_by"], _READER_TIMESTAMP_FORMAT
    ))
    expected = calendar.timegm(time.strptime(
        watch_heartbeat.next_expected_by(1_000_018.0, 80.0, 18.0),
        _READER_TIMESTAMP_FORMAT,
    ))
    assert deadline == expected
    declared_only = calendar.timegm(time.strptime(
        watch_heartbeat.next_expected_by(1_000_018.0, 80.0), _READER_TIMESTAMP_FORMAT
    ))
    assert deadline < declared_only


def test_read_liveness_carries_pid_fields_forward(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, writer_session_id="w1",
    )
    liveness = watch_heartbeat.read_liveness(str(tmp_path))
    assert liveness["pid"] == os.getpid()
    assert liveness["pid_start_epoch"] is not None


# a POLL-ERROR line, never a hang.


def _guard_lock_path(tmp_path):
    path = watch_heartbeat._guard_lock_path(watch_heartbeat.watch_path(str(tmp_path)))
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    return path


def test_a_contended_guard_declines_and_reports_poll_error(tmp_path, capsys):
    lock_path = _guard_lock_path(tmp_path)
    lock_path.write_text(
        json.dumps({"holder_pid": os.getpid(), "hold_until": time.time() + 60.0}),
        encoding="utf-8",
    )
    wrote = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=18.0, writer_session_id="w1",
    )
    assert wrote is False
    err = capsys.readouterr().err
    assert "POLL-ERROR" in err
    assert not os.path.exists(watch_heartbeat.watch_path(str(tmp_path)))
    assert lock_path.exists()


def test_a_stale_guard_is_taken_over_and_the_tick_still_writes(tmp_path):
    lock_path = _guard_lock_path(tmp_path)
    lock_path.write_text(
        json.dumps({"holder_pid": 999_999_999, "hold_until": time.time() + 60.0}),
        encoding="utf-8",
    )
    wrote = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=18.0, writer_session_id="w1",
    )
    assert wrote is True
    assert isinstance(_record(tmp_path), dict)


def test_a_successful_stamp_releases_its_own_guard(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=18.0, writer_session_id="w1",
    )
    assert not _guard_lock_path(tmp_path).exists()


def test_a_declined_stamp_still_releases_its_own_guard(tmp_path):
    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="peer", declinations=[],
        interval_seconds=18.0, writer_session_id="peer-w1",
    )
    wrote = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=18.0, writer_session_id="w1",
    )
    assert wrote is False
    assert not _guard_lock_path(tmp_path).exists()


def test_guard_never_raises_on_a_fresh_unreadable_lock_file_and_declines(tmp_path):
    lock_path = _guard_lock_path(tmp_path)
    lock_path.write_text("not json", encoding="utf-8")
    wrote = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=18.0, writer_session_id="w1",
    )
    assert wrote is False
    assert lock_path.exists()


def test_guard_never_raises_on_a_stale_unreadable_lock_file_and_takes_over(tmp_path):
    lock_path = _guard_lock_path(tmp_path)
    lock_path.write_text("not json", encoding="utf-8")
    old = time.time() - 3600.0
    os.utime(lock_path, (old, old))
    wrote = watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=18.0, writer_session_id="w1",
    )
    assert wrote is True
