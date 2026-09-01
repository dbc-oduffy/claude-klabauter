"""Tests for the standing watch (`coordinator_core/group_em/watch.py`, chunk C2).

Covers: `transitions`' pure not-parked->parked/parked->parked/spawn/exit
table, the obligation-names annotation (`no ledger` vs `none` vs named
records), the cooldown suppression that stops a re-flag of an answered peer,
the POLL-ERROR coverage path, and the measured poll-interval derivation.
"""

from __future__ import annotations

import io
import json
import pathlib

import pytest

from datetime import datetime, timedelta, timezone
from unittest import mock

from coordinator_core.group_em import watch


REPO_ROOT = "/repo/root"


def test_armed_line_names_the_watched_repo_not_a_literal(tmp_path):
    """The ARMED label tracks --repo-root, and a hardcoded repo name is the defect.

    Regression for a live miss measured against the PUBLISHED engine 2026-08-31 by
    doe-claude-80: the line read `claude-klabauter peers` for every --repo-root,
    because the source carried the literal `claude-klabauter` and the publish transform
    rewrites that token on the way into the mirror. So the label was wrong in BOTH
    trees at once and wrong differently in each.

    The counts were correct throughout -- 3 for DoE-claude, 14 for claude-klabauter --
    which is what makes it worth a pin rather than a cosmetic edit. A Group EM arming
    for one repo reads a plausible count beside a FOREIGN repo name, and the honest
    reading is that the watch is pointed at the wrong tree: the failure lands as an
    operator stand-down, not an error. Same family as the guard-messaging rule that
    agent-facing text is a register -- mechanism right, text an operator reads wrong.

    Two roots, because one root cannot distinguish "derived from repo_root" from
    "happens to match this one literal".
    """
    for name in ("alpha-repo", "beta-repo"):
        root = tmp_path / name
        root.mkdir()
        lines: list[str] = []

        class _Stream:
            def write(self, text):
                if text.strip():
                    lines.append(text.strip())

            def flush(self):
                pass

        with mock.patch.object(
            watch, "_measure_snapshot_ms", return_value=(2.0, [{"sessionId": f"p{i}"} for i in range(7)])
        ), mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
            watch.main(
                str(root),
                caller_session_id="caller-1",
                stream=_Stream(),
                sleep_fn=lambda _s: None,
                max_iterations=1,
            )

        assert lines[0].startswith(f"ARMED peer_count=7 {name} peers at "), lines[0]
        assert "claude-klabauter" not in lines[0]
        assert "klabauter" not in lines[0]


# ---------------------------------------------------------------------------
# transitions -- pure function over two boolean maps
# ---------------------------------------------------------------------------


def test_not_parked_to_parked_transitions():
    prev = {"peer-1": False}
    cur = {"peer-1": True}
    assert watch.transitions(prev, cur) == ["peer-1"]


def test_parked_to_parked_emits_nothing():
    prev = {"peer-1": True}
    cur = {"peer-1": True}
    assert watch.transitions(prev, cur) == []


def test_not_parked_to_not_parked_emits_nothing():
    prev = {"peer-1": False}
    cur = {"peer-1": False}
    assert watch.transitions(prev, cur) == []


def test_spawn_already_parked_emits_nothing():
    prev: dict[str, bool] = {}
    cur = {"peer-1": True}
    assert watch.transitions(prev, cur) == []


def test_exit_is_not_a_parked_transition():
    """An exit is `gone`'s event, never `transitions`'.

    The two predicates must not both fire on one departure: a peer that was
    parked last tick and is absent now would otherwise read as PARKED (it is
    still `True` where it is mentioned) AND GONE in the same tick.
    `transitions` requires membership in `cur`; this pins that.
    """
    prev = {"peer-1": True}
    cur: dict[str, bool] = {}
    assert watch.transitions(prev, cur) == []
    assert watch.gone(prev, cur) == ["peer-1"]


def test_multiple_peers_only_the_transitioning_one_is_named():
    prev = {"peer-1": False, "peer-2": True, "peer-3": False}
    cur = {"peer-1": True, "peer-2": True, "peer-3": False}
    assert watch.transitions(prev, cur) == ["peer-1"]


def test_transitions_result_is_sorted():
    prev = {"peer-b": False, "peer-a": False}
    cur = {"peer-b": True, "peer-a": True}
    assert watch.transitions(prev, cur) == ["peer-a", "peer-b"]


# ---------------------------------------------------------------------------
# obligation-names annotation
# ---------------------------------------------------------------------------


def test_obligation_summary_no_ledger():
    with mock.patch.object(watch.obligations, "for_peer", return_value=None):
        assert watch._obligation_summary(REPO_ROOT, "peer-1") == "no ledger"


def test_obligation_summary_empty_ledger():
    with mock.patch.object(watch.obligations, "for_peer", return_value=[]):
        assert watch._obligation_summary(REPO_ROOT, "peer-1") == "none"


def test_obligation_summary_names_records():
    records = [{"obligation_id": "ob-1"}, {"obligation_id": "ob-2"}]
    with mock.patch.object(watch.obligations, "for_peer", return_value=records):
        assert watch._obligation_summary(REPO_ROOT, "peer-1") == "ob-1,ob-2"


def test_obligation_summary_falls_back_to_next_action():
    records = [{"next_action": "ping peer-4a"}]
    with mock.patch.object(watch.obligations, "for_peer", return_value=records):
        assert watch._obligation_summary(REPO_ROOT, "peer-1") == "ping peer-4a"


# ---------------------------------------------------------------------------
# poll_once: end-to-end wiring, cooldown suppression, PARKED line contents
# ---------------------------------------------------------------------------


def _agent(session_id="peer-1", status="idle", cwd=REPO_ROOT, name=None):
    return {"sessionId": session_id, "status": status, "cwd": cwd, "name": name}


def test_poll_once_emits_parked_line_on_transition():
    now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
    emitted = []

    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[_agent()]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PAUSED", "reason": "turn-ended", "candidate": True},
    ), mock.patch.object(
        watch, "_cooldown_active", return_value=False
    ), mock.patch.object(
        watch, "_stamped_age_seconds", return_value=42.0
    ), mock.patch.object(
        watch, "_transcript_idle_seconds", return_value=None
    ), mock.patch.object(
        watch.obligations, "for_peer", return_value=None
    ):
        result, _declinations, _notes = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append)

    assert result == {"peer-1": True}
    assert len(emitted) == 1
    line = emitted[0]
    assert line.startswith("PARKED session=peer-1")
    assert "reason=turn-ended" in line
    assert "stamped_age=42s" in line
    assert "transcript_idle=unknown" in line
    assert "obligations=no ledger" in line


def test_poll_once_stays_silent_on_first_sighting():
    now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
    emitted = []

    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[_agent()]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PAUSED", "reason": "turn-ended", "candidate": True},
    ):
        result, _declinations, _notes = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={}, now=now, emit=emitted.append)

    assert result == {"peer-1": True}
    assert emitted == []


def test_poll_once_suppresses_when_cooldown_active():
    now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
    emitted = []

    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[_agent()]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PAUSED", "reason": "turn-ended", "candidate": True},
    ), mock.patch.object(
        watch, "_cooldown_active", return_value=True
    ):
        result, _declinations, _notes = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append)

    assert result == {"peer-1": True}
    assert emitted == []


def test_poll_once_steady_state_emits_nothing():
    now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
    emitted = []

    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[_agent()]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PAUSED", "reason": "turn-ended", "candidate": True},
    ):
        result, _declinations, _notes = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": True}, now=now, emit=emitted.append)

    assert result == {"peer-1": True}
    assert emitted == []


def test_cooldown_active_reads_send_pass_log():
    records = [{"offer_key": watch.send_pass.offer_key("caller-1", "peer-1"), "offered_at": 1000.0}]
    with mock.patch.object(watch.send_pass, "read_send_log", return_value=records):
        active = watch._cooldown_active(REPO_ROOT, "caller-1", "peer-1", now=1000.0 + 60.0)
    assert active is True


def test_cooldown_inactive_when_no_offer_recorded():
    with mock.patch.object(watch.send_pass, "read_send_log", return_value=[]):
        active = watch._cooldown_active(REPO_ROOT, "caller-1", "peer-1", now=1000.0)
    assert active is False


# ---------------------------------------------------------------------------
# poll-error coverage: a raising poll must never end the watch
# ---------------------------------------------------------------------------


def test_main_emits_poll_error_and_continues(tmp_path):
    """The repo root is a tmp_path, not the module-level `REPO_ROOT` sentinel:
    `main` now writes this repo's heartbeat every tick, so a test driving it
    against `/repo/root` creates that directory on the real filesystem."""
    stream_lines = []

    class _Stream:
        def write(self, text):
            if text.strip():
                stream_lines.append(text.strip())

        def flush(self):
            pass

    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(1.0, [{"sessionId": f"p{i}"} for i in range(3)])
    ), mock.patch.object(
        watch, "poll_once", side_effect=RuntimeError("boom")
    ):
        watch.main(
            str(tmp_path),
            caller_session_id="caller-1",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=2,
        )

    assert any(line.startswith("ARMED peer_count=3") for line in stream_lines)
    assert sum(1 for line in stream_lines if line.startswith("POLL-ERROR")) == 2


def test_main_arms_and_reports_measured_interval(tmp_path):
    stream_lines = []

    class _Stream:
        def write(self, text):
            if text.strip():
                stream_lines.append(text.strip())

        def flush(self):
            pass

    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [{"sessionId": f"p{i}"} for i in range(5)])
    ), mock.patch.object(
        watch, "poll_once", return_value=({}, [], {})
    ):
        watch.main(
            str(tmp_path),
            caller_session_id="caller-1",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
        )

    armed_line = stream_lines[0]
    # The repo name is READ OFF repo_root, so it is tmp_path's basename here rather
    # than a literal. Pinned that way on purpose -- see the regression test below.
    assert armed_line.startswith(f"ARMED peer_count=5 {tmp_path.name} peers at ")
    assert "snapshot=2.0ms" in armed_line
    # interval = max(floor, 1000 * snapshot_s) = max(5.0, 1000 * 0.002) = 5.0
    assert "interval=5.0s" in armed_line


# ---------------------------------------------------------------------------
# poll interval derivation
# ---------------------------------------------------------------------------


def test_poll_interval_floors_at_5_seconds_for_a_fast_snapshot():
    assert watch._poll_interval_seconds(0.1) == 5.0


def test_poll_interval_scales_with_measured_cost_above_the_floor():
    # 1000x multiplier: 10ms measured -> 10s interval.
    assert watch._poll_interval_seconds(10.0) == 10.0


def test_poll_interval_ceilings_a_transient_arm_time_spike():
    # A single bad arm-time sample (e.g. 2000ms) must not commit the watch
    # to a ~33-minute cadence for the whole session.
    assert watch._poll_interval_seconds(2000.0) == watch._POLL_INTERVAL_CEILING_SECONDS


def test_poll_interval_ceiling_is_well_above_a_normal_measurement():
    assert watch._POLL_INTERVAL_CEILING_SECONDS > 30.0


# ---------------------------------------------------------------------------
# ARMABILITY -- the watch must be nameable in a `Monitor` command line, or the
# whole mechanism is inert however green its unit tests are.
# ---------------------------------------------------------------------------


def test_module_has_a_command_line_entrypoint():
    """`Monitor` takes a COMMAND, so a module with only an importable `main()`
    cannot be armed. This test exists because that is exactly how this module
    first shipped: fully tested, fully inert."""
    import coordinator_core.group_em.watch as watch_mod

    assert hasattr(watch_mod, "_cli")
    source = pathlib.Path(watch_mod.__file__).read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in source


def test_cli_requires_repo_root_rather_than_guessing_cwd(capsys):
    """The watch runs under a harness tool whose working directory is not ours,
    so a cwd default would silently watch the wrong tree."""
    with pytest.raises(SystemExit) as excinfo:
        watch._cli([])
    assert excinfo.value.code != 0
    assert "--repo-root" in capsys.readouterr().err


def test_cli_runs_a_bounded_watch_and_emits_armed(tmp_path, monkeypatch, capsys):
    """End to end through the entrypoint an operator would actually type."""
    monkeypatch.setattr(watch, "_measure_snapshot_ms", lambda repo_root: (4.6, [{"sessionId": f"p{i}"} for i in range(3)]))
    monkeypatch.setattr(watch, "_current_agents", lambda repo_root, sid: [])
    rc = watch._cli(
        ["--repo-root", str(tmp_path), "--caller-session-id", "sid-cli", "--max-iterations", "1"]
    )
    assert rc == 0
    assert "ARMED" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# two ids: the watching process is not always the Group-EM
# (cross-repo/inbox/2026-08-31-doe-claude-em-fleet-watch-needs-engine-side-
# transition-events.md, question 1 -- DoE stands up a teammate to hold this
# watch so the Group-EM's context stays free for adjudicating)
# ---------------------------------------------------------------------------


def test_roster_excludes_both_the_watcher_and_the_group_em():
    """A teammate sitting in a `Monitor` poll presents exactly like a parked
    peer, and the Group-EM is the recipient of every line -- neither belongs in
    the watched set."""
    agents = [
        {"sessionId": "watcher-1", "status": "idle", "cwd": REPO_ROOT},
        {"sessionId": "group-em-1", "status": "idle", "cwd": REPO_ROOT},
        {"sessionId": "peer-1", "status": "idle", "cwd": REPO_ROOT},
    ]
    with mock.patch.object(watch.read_pass, "fetch_live_agents", return_value=agents):
        peers = watch._current_agents(REPO_ROOT, "watcher-1", "group-em-1")
    assert [p["sessionId"] for p in peers] == ["peer-1"]


def test_cooldown_is_read_off_the_group_ems_send_log_not_the_watchers():
    """The offer log is per-session on disk, so a watcher reading its own
    empty log would re-flag every peer the Group-EM already answered."""
    seen = {}

    def _fake_read_send_log(repo_root, caller_session_id):
        seen["caller"] = caller_session_id
        return []

    agents = [{"sessionId": "peer-1", "status": "idle", "cwd": REPO_ROOT}]
    verdict = {"session_id": "peer-1", "candidate": True, "reason": "turn-ended"}
    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=agents
    ), mock.patch.object(
        watch.read_pass, "classify_peer", return_value=verdict
    ), mock.patch.object(
        watch.send_pass, "read_send_log", side_effect=_fake_read_send_log
    ), mock.patch.object(
        watch, "_parked_line", return_value="PARKED session=peer-1"
    ):
        watch.poll_once(
            REPO_ROOT,
            "watcher-1",
            {"peer-1": False},
            emit=lambda _line: None,
            group_em_session_id="group-em-1",
        )

    assert seen["caller"] == "group-em-1"


def test_group_em_session_id_defaults_to_the_calling_session():
    """The ordinary case -- the Group-EM holds the poller itself -- keeps
    working with one id, unchanged."""
    seen = {}

    def _fake_read_send_log(repo_root, caller_session_id):
        seen["caller"] = caller_session_id
        return []

    agents = [{"sessionId": "peer-1", "status": "idle", "cwd": REPO_ROOT}]
    verdict = {"session_id": "peer-1", "candidate": True, "reason": "turn-ended"}
    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=agents
    ), mock.patch.object(
        watch.read_pass, "classify_peer", return_value=verdict
    ), mock.patch.object(
        watch.send_pass, "read_send_log", side_effect=_fake_read_send_log
    ), mock.patch.object(
        watch, "_parked_line", return_value="PARKED session=peer-1"
    ):
        watch.poll_once(REPO_ROOT, "caller-1", {"peer-1": False}, emit=lambda _line: None)

    assert seen["caller"] == "caller-1"


# ---------------------------------------------------------------------------
# the presence stamp: arming this watch must not read as no watch at all
# (same memo, question 2)
# ---------------------------------------------------------------------------


def test_declinations_record_the_gate_that_stopped_each_peer():
    agents = [
        {"sessionId": "parked-1", "status": "idle", "cwd": REPO_ROOT},
        {"sessionId": "busy-1", "status": "busy", "cwd": REPO_ROOT},
    ]
    verdicts = {
        "parked-1": {"session_id": "parked-1", "candidate": True, "reason": "turn-ended"},
        "busy-1": {"session_id": "busy-1", "candidate": False, "reason": "producing"},
    }
    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=agents
    ), mock.patch.object(
        watch.read_pass, "classify_peer", side_effect=lambda repo_root, peer, **kw: verdicts[peer["sessionId"]]
    ), mock.patch.object(
        watch, "_cooldown_active", return_value=True
    ):
        _parked, declinations, _notes = watch.poll_once(
            REPO_ROOT,
            "caller-1",
            {"parked-1": False, "busy-1": False},
            emit=lambda _line: None,
        )

    rows = {row["session_id"]: row for row in declinations}
    assert rows["parked-1"]["gate"] == "cooldown"
    assert rows["busy-1"]["gate"] == "not-a-candidate"
    assert rows["busy-1"]["reason"] == "producing"
    assert all(row["name"] is None for row in declinations)


def test_main_stamps_the_watch_presence_record_every_tick(tmp_path):
    """A Group-EM that arms this runnable and stops hand-stamping must not read
    to the rest of the fleet as a repo nobody is watching."""
    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [{"sessionId": f"p{i}"} for i in range(5)])
    ), mock.patch.object(
        watch, "poll_once", return_value=({}, [], {})
    ):
        watch.main(
            str(tmp_path),
            caller_session_id="watcher-1",
            group_em_session_id="group-em-1",
            stream=io.StringIO(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
        )

    with open(watch.watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["holder_session_id"] == "group-em-1"
    assert record["tick_source"] == "monitor"


def test_parked_line_reuses_the_verdicts_epoch_and_reads_no_second_time():
    """The transcript was already reduced once by `classify_peer` this tick;
    re-deriving the same number here was a duplicate seek-from-EOF read
    (coordinator:code-reviewer, P2)."""
    now = datetime(2026, 8, 31, 16, 0, 0, tzinfo=timezone.utc)
    verdict = {
        "session_id": "peer-1",
        "candidate": True,
        "reason": "turn-ended",
        "activity_epoch": now.timestamp() - 300.0,
    }

    with mock.patch.object(
        watch.read_pass, "transcript_activity_epoch", side_effect=AssertionError("re-read")
    ), mock.patch.object(
        watch, "_stamped_age_seconds", return_value=None
    ), mock.patch.object(
        watch, "_obligation_summary", return_value="none"
    ):
        line = watch._parked_line(REPO_ROOT, "peer-1", verdict, REPO_ROOT, now)

    assert "transcript_idle=300s" in line


def test_parked_line_still_reads_when_the_verdict_carries_no_epoch():
    """The reader leg reduces no tail, so there is nothing to reuse -- paying
    one read there is a first read, not a second."""
    now = datetime(2026, 8, 31, 16, 0, 0, tzinfo=timezone.utc)
    verdict = {"session_id": "peer-1", "candidate": True, "reason": "turn-ended"}

    with mock.patch.object(
        watch.read_pass,
        "transcript_activity_epoch",
        return_value=(now.timestamp() - 120.0, True),
    ), mock.patch.object(
        watch, "_stamped_age_seconds", return_value=None
    ), mock.patch.object(
        watch, "_obligation_summary", return_value="none"
    ):
        line = watch._parked_line(REPO_ROOT, "peer-1", verdict, REPO_ROOT, now)

    assert "transcript_idle=120s" in line


# --- the single-tick wake (`--once` / `tick_once`) -------------------------
#
# The mode exists because a watch that must HOLD a process to be watching has
# one failure mode and no signal for it: a subprocess that never started, or
# exited, or an agent that returned instead of blocking, reads from outside
# exactly like a quiet fleet. Measured live 2026-09-01 from
# example-game-workbench-repo (cross-repo memo group-em-fleet-watch-wake-on-session-state).
# What these pin is the ONE thing the held loop got for free and a stateless
# wake has to earn: the prior tick to diff against.


def _parked_once(repo_root, prev_on_disk, candidate, emitted, now=None):
    """Drive one `tick_once` over a single peer with a fixed verdict.

    The prior map is stamped with the tick's OWN `now`, never the wall clock:
    a real `time.time()` here makes the staleness gate a function of what
    hour the suite runs in, and these cases are about the parked transition,
    not about how old the carried state is.
    """
    now = now or datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    watch.save_prev_parked(str(repo_root), prev_on_disk)
    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[_agent()]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PAUSED", "reason": "turn-ended", "candidate": candidate},
    ), mock.patch.object(
        watch, "_cooldown_active", return_value=False
    ), mock.patch.object(
        watch, "_stamped_age_seconds", return_value=42.0
    ), mock.patch.object(
        watch, "_transcript_idle_seconds", return_value=None
    ), mock.patch.object(
        watch.obligations, "for_peer", return_value=None
    ):
        stream = io.StringIO()
        rc = watch.tick_once(
            str(repo_root),
            caller_session_id="waker-1",
            group_em_session_id="group-em-1",
            stream=stream,
            now=now,
        )
    emitted.extend(l for l in stream.getvalue().splitlines() if l.strip())
    return rc


def test_tick_once_carries_the_prior_tick_across_two_separate_wakes(tmp_path):
    """Two processes, no shared memory, one transition -- reported once.

    This is the whole contract. Wake one sees an unparked peer and says
    nothing; wake two, a different process entirely, sees it parked and emits
    the line, because the first wrote down what it saw.
    """
    emitted: list[str] = []
    assert _parked_once(tmp_path, {}, candidate=False, emitted=emitted) == 0
    assert emitted == []

    assert _parked_once(tmp_path, watch.load_prev_parked(str(tmp_path)), candidate=True, emitted=emitted) == 0
    assert len(emitted) == 1
    assert emitted[0].startswith("PARKED session=peer-1")


def test_tick_once_is_silent_when_the_peer_was_already_parked_last_wake(tmp_path):
    """Steady state emits nothing -- the firehose that gets a watch muted."""
    emitted: list[str] = []
    _parked_once(tmp_path, {"peer-1": True}, candidate=True, emitted=emitted)
    assert emitted == []


def test_tick_once_stamps_the_presence_record_with_the_cron_word(tmp_path):
    """A reader must be able to tell a wake-driven watch from a held poller:
    what happens next if nobody fires again is a different question for each."""
    _parked_once(tmp_path, {}, candidate=False, emitted=[])
    with open(watch.watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["tick_source"] == "cron"
    assert record["holder_session_id"] == "group-em-1"


def test_tick_once_deadline_follows_the_callers_cadence_not_the_poll_interval(tmp_path):
    """A wake that stamped the poll loop's few-second interval would read STALE
    within the minute -- the watch reporting itself absent while working."""
    with mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        watch.tick_once(str(tmp_path), caller_session_id="w", group_em_session_id="c", stream=io.StringIO())
    with open(watch.watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.load(fh)
    last = datetime.strptime(record["last_tick_at"], "%Y-%m-%dT%H:%M:%SZ")
    nxt = datetime.strptime(record["next_expected_by"], "%Y-%m-%dT%H:%M:%SZ")
    assert (nxt - last).total_seconds() >= watch._CRON_FLOOR_INTERVAL_SECONDS


def test_tick_once_exits_nonzero_and_loud_when_the_poll_raises(tmp_path):
    """No loop to carry on into: a wake that failed must not exit 0. A silent
    zero here rebuilds the exact indistinguishability this mode removes."""
    stream = io.StringIO()
    with mock.patch.object(watch, "poll_once", side_effect=RuntimeError("registry gone")):
        rc = watch.tick_once(str(tmp_path), caller_session_id="w", group_em_session_id="c", stream=stream)
    assert rc == 1
    assert "POLL-ERROR" in stream.getvalue()


def test_load_prev_parked_answers_empty_for_absent_and_malformed_state(tmp_path):
    """An unreadable prior map must read as a first tick, never as a roster to
    flag wholesale."""
    assert watch.load_prev_parked(str(tmp_path)) == {}
    state = pathlib.Path(watch.parked_state_path(str(tmp_path)))
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json", encoding="utf-8")
    assert watch.load_prev_parked(str(tmp_path)) == {}
    state.write_text('{"parked": "everyone"}', encoding="utf-8")
    assert watch.load_prev_parked(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# GONE -- the departure event, added 2026-09-01.
#
# Until this landed, a session disappearing from the roster emitted nothing on
# the surface that ticks: the `exited` list exists, but only inside
# `baseline.diff_and_persist`, reached from the once-per-`/group-em` entry op.
# So the only working detector of a departed peer was failing to send to it,
# measured twice in this repo on 2026-09-01 -- `claude-klabauter-c7` listed by
# `ListAgents` and refusing a `SendMessage` seconds later, and
# `claude-klabauter-3e` vanishing mid-workstream with nothing announced.
# ---------------------------------------------------------------------------


def test_gone_names_only_peers_that_left():
    prev = {"peer-a": False, "peer-b": True, "peer-c": False}
    cur = {"peer-a": False, "peer-c": True}
    assert watch.gone(prev, cur) == ["peer-b"]


def test_gone_is_sorted_and_ignores_the_prior_parked_value():
    """Parked-when-last-seen is not part of the predicate: a peer that was
    working when it left has left exactly as much as one that was parked."""
    assert watch.gone({"peer-b": False, "peer-a": True}, {}) == ["peer-a", "peer-b"]


def test_gone_says_nothing_about_a_spawn():
    assert watch.gone({}, {"peer-1": True}) == []


def test_gone_line_carries_the_name_and_the_gap():
    """The name is the one field a reader cannot recover afterwards -- the
    roster row that held it is gone by the time this fires, and `SendMessage`
    takes a name, not a uuid."""
    now = datetime(2026, 9, 1, 12, 5, 0, tzinfo=timezone.utc)
    line = watch._gone_line(
        "sid-c7",
        "claude-klabauter",
        now,
        name="claude-klabauter-c7",
        last_seen_epoch=datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp(),
    )
    assert line.startswith("GONE session=claude-klabauter-c7 [sid-c7]")
    assert "last_seen=2026-09-01T12:00:00Z" in line
    assert "gap=300s" in line
    assert "claude-klabauter's roster" in line
    assert "do not send" in line


def test_gone_line_says_unknown_rather_than_inventing_a_last_seen():
    """A record written before `last_seen` existed must not render as a fresh
    exit -- `now` here would be a default wearing a measurement's clothes."""
    now = datetime(2026, 9, 1, 12, 5, 0, tzinfo=timezone.utc)
    line = watch._gone_line("sid-1", "repo", now, name=None, last_seen_epoch=None)
    assert "last_seen=unknown" in line
    assert "gap=" not in line
    assert line.startswith("GONE session=sid-1 ")


def _gone_poll(prev_parked, agents, prev_names=None, emitted=None, **kwargs):
    """Drive one `poll_once` over a fixed roster with everything not-parked."""
    emitted = emitted if emitted is not None else []
    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=agents
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda a, sid: [x for x in a if x.get("sessionId") != sid]
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PRODUCING", "reason": "tool-use", "candidate": False},
    ):
        result = watch.poll_once(
            REPO_ROOT,
            "waker-1",
            prev_parked,
            now=datetime(2026, 9, 1, 12, 5, 0, tzinfo=timezone.utc),
            emit=emitted.append,
            group_em_session_id="group-em-1",
            prev_names=prev_names,
            **kwargs,
        )
    return result, emitted


def test_poll_once_reports_a_departed_peer_by_name():
    last_seen = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    (_parked, _decl, notes), emitted = _gone_poll(
        prev_parked={"peer-1": False, "peer-gone": False},
        agents=[_agent("peer-1")],
        prev_names={"peer-gone": {"name": "claude-klabauter-3e", "last_seen": last_seen}},
    )
    assert len(emitted) == 1
    assert emitted[0].startswith("GONE session=claude-klabauter-3e [peer-gone]")
    assert "gap=300s" in emitted[0]
    # The surviving peer's note is carried forward for the NEXT tick to name.
    assert set(notes) == {"peer-1"}
    assert notes["peer-1"]["last_seen"] == pytest.approx(
        datetime(2026, 9, 1, 12, 5, 0, tzinfo=timezone.utc).timestamp()
    )


def test_poll_once_still_reports_a_departure_it_cannot_name():
    """An unnameable departure is still worth a line -- silence was the defect,
    and the uuid at least tells a reader which roster row to drop."""
    (_p, _d, _n), emitted = _gone_poll(
        prev_parked={"peer-gone": True}, agents=[], prev_names=None
    )
    assert len(emitted) == 1
    assert emitted[0].startswith("GONE session=peer-gone ")
    assert "last_seen=unknown" in emitted[0]


def test_poll_once_never_reports_the_watcher_or_the_group_em_as_gone():
    """Both are excluded from every roster this module builds, so neither can
    legitimately appear -- but a wake handed a DIFFERENT --group-em-session-id
    than the last one changes the exclusion set, and reporting the Group-EM as
    gone to the Group-EM is the worst possible way to say a flag changed."""
    (_p, _d, _n), emitted = _gone_poll(
        prev_parked={"waker-1": False, "group-em-1": False, "peer-1": False},
        agents=[],
    )
    assert len(emitted) == 1
    assert "peer-1" in emitted[0]


def test_an_unreadable_registry_raises_rather_than_reporting_the_fleet_gone():
    """THE worst false positive this line can produce, and it fires exactly
    when the box is least healthy: `fetch_live_agents` degrades an unreadable
    registry to `[]`, which a differ reads as a simultaneous mass exit. The
    watch reads with `raise_on_failure=True` so the failure becomes a
    POLL-ERROR line and the prior map is left unwritten -- the next tick diffs
    against the last GOOD roster, not against a hole.
    """
    emitted: list[str] = []
    with mock.patch.object(
        watch.read_pass.peer_roster.harness_registry,
        "snapshot",
        side_effect=OSError("registry unreadable"),
    ):
        with pytest.raises(OSError):
            watch.poll_once(
                REPO_ROOT,
                "waker-1",
                {"peer-1": False, "peer-2": False},
                emit=emitted.append,
            )
    assert emitted == []


def test_an_empty_box_wide_snapshot_raises_rather_than_reading_as_a_drained_fleet():
    """THE FAILURE THAT ACTUALLY FIRES, and the first version of this guard
    missed it.

    `harness_registry.snapshot()` catches every internal failure at its own
    boundary -- an absent or unresolvable registry directory included -- and
    answers `{}` by explicit contract. It structurally cannot raise, so
    `raise_on_failure` alone (which only re-raises exceptions) never sees the
    outage: it arrives as an empty dict and reads as an empty box.

    Established by `example-game-workbench-repo-95`, 2026-09-01: their fleet
    instrument armed into a registry outage and reported a healthy `peers: 0`
    every heartbeat for 22 minutes while `ListAgents` showed 36 sessions
    throughout. This drives the REAL chain -- no patch on `fetch_live_agents`
    or `build_roster` -- so it fails if the flag stops being threaded.
    """
    emitted: list[str] = []
    with mock.patch.object(
        watch.read_pass.peer_roster.harness_registry, "snapshot", return_value={}
    ):
        with pytest.raises(watch.read_pass.peer_roster.EmptySnapshotError):
            watch.poll_once(REPO_ROOT, "waker-1", {"peer-1": False}, emit=emitted.append)
    assert emitted == []


def test_an_empty_snapshot_is_still_a_quiet_empty_list_for_everyone_else():
    """The refusal is opt-in and box-wide-only. Every existing caller keeps
    the degrade-to-`[]` contract, and a repo with no peers is not an outage."""
    with mock.patch.object(
        watch.read_pass.peer_roster.harness_registry, "snapshot", return_value={}
    ):
        assert watch.read_pass.peer_roster.build_roster(repo_root=REPO_ROOT) == []
        assert watch.read_pass.fetch_live_agents(REPO_ROOT) == []


def test_a_blind_tick_stamps_no_heartbeat_and_keeps_the_last_good_prior(tmp_path):
    """A FAILED READ MUST NOT BE PUBLISHED AS A COVERAGE FIGURE.

    The same defect one level up, and the more dangerous half: a heartbeat
    carrying `peers: 0` says "I looked and the fleet is empty" in the voice of
    "all well", and it retires the suspicion that would have caught it.
    95 found out by reading the file by hand, 17 minutes after the instrument
    should have told them.

    Two assertions, because the stamp and the prior map are two separate ways
    to publish a blind tick as truth: nothing is stamped at all (so the record
    ages and `--status` answers STALE rather than a confident zero), and the
    carried map still holds the last GOOD roster, so the next tick diffs
    against a fleet rather than against a hole.
    """
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    watch.save_prev_parked(
        str(tmp_path),
        {"peer-1": False, "peer-2": False},
        peers={"peer-1": {"name": "claude-klabauter-01", "last_seen": now.timestamp()}},
    )
    stream = io.StringIO()
    with mock.patch.object(
        watch.read_pass.peer_roster.harness_registry, "snapshot", return_value={}
    ):
        rc = watch.tick_once(
            str(tmp_path),
            caller_session_id="waker-1",
            group_em_session_id="group-em-1",
            stream=stream,
            now=now,
        )

    assert rc == 1
    assert "POLL-ERROR" in stream.getvalue()
    assert "GONE" not in stream.getvalue()
    assert not pathlib.Path(watch.watch_heartbeat.watch_path(str(tmp_path))).exists()
    assert watch.load_prev_parked(str(tmp_path)) == {"peer-1": False, "peer-2": False}


def test_no_spawn_event_exists_so_a_mass_arrival_cannot_be_reported():
    """The mirror of a mass exit is a mass spawn -- an empty `prev` meeting a
    recovered `cur` -- and it is the worse alarm, because a fleet that just
    got busy reads as healthy and nobody looks.

    This module emits no NEW event at all, so there is nothing to guard
    rather than a guard that was skipped. Pinned so that anything adding a
    spawn line here has to come back and set the same refusals: with 36 peers
    arriving against an empty prior, the correct output is still silence.
    """
    (_p, _d, _n), emitted = _gone_poll(
        prev_parked={}, agents=[_agent(f"sid-{i}") for i in range(36)]
    )
    assert emitted == []


def test_a_genuinely_empty_repo_still_reports_its_departures():
    """The flag must separate "unreadable" from "empty", not collapse both into
    a raise -- a fleet that really did drain is exactly what GONE is for."""
    (_p, _d, _n), emitted = _gone_poll(prev_parked={"peer-1": False}, agents=[])
    assert len(emitted) == 1
    assert "peer-1" in emitted[0]


def test_the_carried_state_round_trips_names(tmp_path):
    watch.save_prev_parked(
        str(tmp_path),
        {"peer-1": True},
        peers={"peer-1": {"name": "claude-klabauter-01", "last_seen": 1000.0}},
    )
    assert watch.load_prev_parked(str(tmp_path)) == {"peer-1": True}
    assert watch.load_prev_peers(str(tmp_path))["peer-1"]["name"] == "claude-klabauter-01"


def test_a_pre_2026_09_01_record_degrades_to_no_names_not_to_no_gone(tmp_path):
    """The parked map alone is a sufficient prior peer SET. A record without
    `peers` must still yield its departures, unnamed."""
    state = pathlib.Path(watch.parked_state_path(str(tmp_path)))
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"parked": {"peer-1": false}}', encoding="utf-8")
    assert watch.load_prev_parked(str(tmp_path)) == {"peer-1": False}
    assert watch.load_prev_peers(str(tmp_path)) == {}


def _gone_tick(repo_root, agents, emitted, now, tick_interval_seconds=None):
    """Drive one `tick_once` over a fixed roster, everything not-parked."""
    kwargs = {} if tick_interval_seconds is None else {"tick_interval_seconds": tick_interval_seconds}
    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=agents
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda a, sid: [x for x in a if x.get("sessionId") != sid]
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PRODUCING", "reason": "tool-use", "candidate": False},
    ):
        stream = io.StringIO()
        rc = watch.tick_once(
            str(repo_root),
            caller_session_id="waker-1",
            group_em_session_id="group-em-1",
            stream=stream,
            now=now,
            **kwargs,
        )
    emitted.extend(l for l in stream.getvalue().splitlines() if l.strip())
    return rc


def test_two_stateless_wakes_report_a_departure_by_the_name_the_first_one_saw():
    """The end-to-end contract, across two processes with no shared memory.

    This is the case the fleet actually runs: wake one sees a peer and writes
    its name down; wake two, a different process, sees it absent and can still
    say WHO left -- which the roster it is reading no longer knows.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        emitted: list[str] = []
        t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert _gone_tick(root, [_agent("sid-3e", name="claude-klabauter-3e")], emitted, t0) == 0
        assert emitted == []

        assert _gone_tick(root, [], emitted, t0 + timedelta(minutes=5)) == 0
        assert len(emitted) == 1
        assert emitted[0].startswith("GONE session=claude-klabauter-3e [sid-3e]")
        assert "last_seen=2026-09-01T12:00:00Z" in emitted[0]
        assert "gap=300s" in emitted[0]


def test_a_wake_diffing_against_a_long_dead_watchs_map_reports_every_departure():
    """A watch restarted after an outage diffs against a map from before it --
    every peer that turned over in the gap is a real, truthful departure.

    STALE-PRIOR (a per-tick suppression of these lines, keyed off the prior
    map's on-disk age) was deleted -- overengineering-reviewer finding #1,
    accepted: GONE is terminal and self-limiting (each session id can only
    ever be reported gone once, module docstring), so a burst after an outage
    is N truthful lines, once, never the repeating firehose the Monitor
    auto-stop guards against. Nothing replaces the suppression; this test
    pins that the burst is reported in full rather than collapsed.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        emitted: list[str] = []
        t0 = datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc)
        _gone_tick(root, [_agent(f"sid-{i}") for i in range(3)], emitted, t0, tick_interval_seconds=60.0)
        emitted.clear()

        # Ten hours later: the prior map is old, but every departure is real.
        _gone_tick(root, [], emitted, t0 + timedelta(hours=10), tick_interval_seconds=60.0)
        assert len(emitted) == 3
        assert all(line.startswith("GONE") for line in emitted)
        assert not any(line.startswith("STALE-PRIOR") for line in emitted)

        # And the very next tick is clean, as it always was.
        emitted.clear()
        _gone_tick(root, [_agent("sid-new")], emitted, t0 + timedelta(hours=10, minutes=1), tick_interval_seconds=60.0)
        assert emitted == []


def test_a_departure_across_two_wakes_is_reported_whatever_the_cadence():
    """Renamed when STALE-PRIOR was deleted (overengineering review, finding 1).

    It was `..._inside_its_own_cadence_is_not_stale`, naming a staleness gate
    that no longer exists -- a test named for a deleted mechanism reads as
    coverage of it. The behaviour it actually pins survived the deletion and
    is worth keeping: two stateless wakes, a peer present then absent, one
    GONE line. `tick_interval_seconds` is now only the heartbeat's staleness
    deadline and no longer gates whether the departure is reported at all,
    which is the simplification this test now documents by passing unchanged.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        emitted: list[str] = []
        t0 = datetime(2026, 9, 1, 2, 0, 0, tzinfo=timezone.utc)
        _gone_tick(root, [_agent("sid-1")], emitted, t0, tick_interval_seconds=60.0)
        emitted.clear()
        _gone_tick(root, [], emitted, t0 + timedelta(seconds=90), tick_interval_seconds=60.0)
        assert len(emitted) == 1
        assert emitted[0].startswith("GONE session=sid-1")


def test_gone_emits_even_when_persistence_raises(tmp_path):
    """Review: coordinatorcode-reviewer.a933f243c20654e60, Finding 1 -- pins
    the emit-then-persist ordering as deliberate, not incidental.

    `poll_once` emits its GONE line INSIDE the call, before `save_prev_parked`
    ever runs. If persistence then raises, the departed peer is never retired
    from the on-disk prior map, so the SAME departure is reported again next
    tick -- a duplicate, not a loss. This is the accepted-by-design tradeoff:
    a duplicate GONE is noise a reader can discard; the rejected alternative
    (persist-then-emit) would instead retire the peer from the map before any
    line was printed, so the same failure would silently DROP the departure
    instead -- the exact failure this module exists to remove. This test
    proves only the half that matters for `tick_once`'s own contract: emission
    does not depend on persistence succeeding.
    """
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    watch.save_prev_parked(str(tmp_path), {"peer-1": False})

    stream = io.StringIO()
    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda a, sid: a
    ), mock.patch.object(
        watch, "save_prev_parked", side_effect=OSError("disk full")
    ):
        with pytest.raises(OSError):
            watch.tick_once(
                str(tmp_path),
                caller_session_id="waker-1",
                group_em_session_id="group-em-1",
                stream=stream,
                now=now,
            )

    assert "GONE session=peer-1" in stream.getvalue()


def test_cli_once_runs_one_tick_and_returns_its_code(tmp_path):
    """`--once` must not fall through into the held loop -- the flag IS the mode."""
    with mock.patch.object(watch, "tick_once", return_value=0) as ticker, mock.patch.object(
        watch, "main", side_effect=AssertionError("--once must not arm the poll loop")
    ):
        rc = watch._cli(["--repo-root", str(tmp_path), "--group-em-session-id", "group-em-1", "--once"])
    assert rc == 0
    assert ticker.call_args.kwargs["group_em_session_id"] == "group-em-1"


# --- what the heartbeat says about itself ----------------------------------


def test_the_record_names_the_holder_and_the_coverage_it_actually_had(tmp_path):
    """Two facts a reader could not get from the record before.

    `holder_name` was hardcoded null, so a reader that could not reach this
    box's registry -- another machine, the record read cold -- had a session
    id and nothing else, exactly when self-description is all that is left.
    `subscribed_peers` was the default 1 on every tick: the Group-EM of this repo
    read `subscribed_peers: 1` against a live population of 10-18 on
    2026-09-01, and a watch covering one peer looked identical to a healthy
    one on every artifact on disk.
    """
    agents = [{"sessionId": "group-em-1", "name": "claude-klabauter-65"}]
    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, agents)
    ), mock.patch.object(
        watch, "poll_once", return_value=({"p1": True, "p2": False, "p3": False}, [], {})
    ):
        watch.main(
            str(tmp_path),
            caller_session_id="watcher-1",
            group_em_session_id="group-em-1",
            stream=io.StringIO(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
        )

    with open(watch.watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["holder_name"] == "claude-klabauter-65"
    assert record["subscribed_peers"] == 3


def test_the_holder_name_is_resolved_once_at_arm_not_per_tick(tmp_path):
    """A name on a heartbeat is self-description, not an address. Paying a
    registry read every tick to keep a string fresh puts the load norm's cost
    on the cheapest thing the watch does."""
    agents = [{"sessionId": "group-em-1", "name": "claude-klabauter-65"}]
    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, agents)
    ) as measured, mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        watch.main(
            str(tmp_path),
            caller_session_id="watcher-1",
            group_em_session_id="group-em-1",
            stream=io.StringIO(),
            sleep_fn=lambda _s: None,
            max_iterations=4,
        )
    assert measured.call_count == 1


def test_a_nameless_wake_carries_the_armed_pollers_name_rather_than_blanking_it(tmp_path):
    """`--once` makes no enumeration, so it has no name to write. Writing null
    would make the record oscillate between describing itself and not,
    depending on which clock last fired."""
    watch.watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="group-em-1",
        declinations=[],
        interval_seconds=5.0,
        holder_name="claude-klabauter-65",
        writer_session_id="w",
        tick_source="cron",
    )
    with mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        watch.tick_once(
            str(tmp_path), caller_session_id="w", group_em_session_id="group-em-1", stream=io.StringIO()
        )
    with open(watch.watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["holder_name"] == "claude-klabauter-65"
    assert record["tick_source"] == "cron"


def test_a_new_holders_stamp_does_not_inherit_the_old_holders_name(tmp_path):
    """A stale name beside a new id is worse than no name: it is an address
    that resolves to the wrong session."""
    watch.watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="group-em-1",
        declinations=[],
        interval_seconds=5.0,
        holder_name="claude-klabauter-65",
        writer_session_id="w1",
        now_epoch=1_000_000.0,
    )
    watch.watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="Group-EM-2", declinations=[], interval_seconds=5.0,
        writer_session_id="w1", now_epoch=1_000_100.0,
    )
    with open(watch.watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["holder_session_id"] == "Group-EM-2"
    assert record["holder_name"] is None


def test_the_parked_line_names_the_peer_and_frames_the_name_as_provenance():
    """A reader cannot act on a session uuid -- `SendMessage` takes a name --
    so a line carrying only the uuid asks the Group-EM to resolve one from a
    registry this tick already read. The name goes on, framed as how the peer
    was known THIS TICK, with `verify before sending` rather than
    `re-resolve from this id`: in the case that matters the printed id is
    exactly the one that no longer resolves."""
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    emitted = []
    agent = dict(_agent(), name="claude-klabauter-65")

    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[agent]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PAUSED", "reason": "turn-ended", "candidate": True},
    ), mock.patch.object(
        watch, "_cooldown_active", return_value=False
    ), mock.patch.object(
        watch, "_stamped_age_seconds", return_value=42.0
    ), mock.patch.object(
        watch, "_transcript_idle_seconds", return_value=None
    ), mock.patch.object(
        watch.obligations, "for_peer", return_value=None
    ):
        watch.poll_once(
            REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append
        )

    line = emitted[0]
    assert "session=claude-klabauter-65 [peer-1]" in line
    assert "verify before sending" in line
    assert "re-resolve" not in line


def test_a_nameless_peer_still_gets_a_line():
    """A missing name degrades to the bare id -- never to silence, and never
    to an invented address."""
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    emitted = []

    with mock.patch.object(
        watch.read_pass, "fetch_live_agents", return_value=[_agent()]
    ), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda agents, sid: agents
    ), mock.patch.object(
        watch.read_pass,
        "classify_peer",
        return_value={"state": "PAUSED", "reason": "turn-ended", "candidate": True},
    ), mock.patch.object(
        watch, "_cooldown_active", return_value=False
    ), mock.patch.object(
        watch, "_stamped_age_seconds", return_value=None
    ), mock.patch.object(
        watch, "_transcript_idle_seconds", return_value=None
    ), mock.patch.object(
        watch.obligations, "for_peer", return_value=None
    ):
        watch.poll_once(
            REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append
        )

    assert emitted[0].startswith("PARKED session=peer-1 ")


# --- the root the process stands on ----------------------------------------


def test_the_cli_refuses_a_root_that_does_not_exist(capsys):
    """`peer_count=0` is what a quiet repo and an unreadable one both printed,
    and the run exited 0. A watcher reported `armed and standing by` four times
    across fifty minutes while watching nothing, through nine live peer
    transitions (example-game-repo-em, 2026-09-01)."""
    rc = watch._cli(["--repo-root", "X:/no-such-repo-anywhere"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not an existing directory" in err
    assert "no-such-repo-anywhere" in err


def test_the_cli_refuses_a_drive_relative_root(capsys):
    """The mangling that actually happened: a Windows backslash path through a
    shell loses its separators, leaving `X:name` -- drive-relative, so it binds
    to wherever the process was standing. A type check cannot see it; only
    `isabs` can, and it must refuse even when the directory it would resolve to
    happens to exist."""
    rc = watch._cli(["--repo-root", "X:example-game-workbench-repo"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not an absolute, drive-anchored path" in err
    assert "resolves to" in err


def test_the_cli_refuses_a_driveless_rooted_posix_style_path(capsys):
    """`ntpath.isabs('/foo/bar')` is True with no drive component -- `abspath`
    then resolves it against the process's CURRENT DRIVE, the same "binds to
    wherever the process happens to be standing" hazard as the drive-relative
    case, just swapping drive for directory (coordinator:code-reviewer,
    a1574022171f8f1cc, P2)."""
    rc = watch._cli(["--repo-root", "/foo/bar"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not an absolute, drive-anchored path" in err


def test_the_armed_line_names_the_resolved_path_not_only_the_repo_name(tmp_path):
    """The display name SURVIVES the mangling -- it is the tail of the mangled
    path too -- so the name alone made a broken arm look healthy. The resolved
    path is what makes the line self-diagnosing."""
    lines: list[str] = []

    class _Stream:
        def write(self, text):
            if text.strip():
                lines.append(text.strip())

        def flush(self):
            pass

    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [])
    ), mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        watch.main(
            str(tmp_path),
            caller_session_id="sid",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
        )

    armed = lines[0]
    assert armed.startswith("ARMED ")
    assert str(pathlib.Path(tmp_path).resolve()) in armed


def test_cli_status_answers_alive_for_a_fresh_record_and_exits_zero(tmp_path, capsys):
    from coordinator_core.group_em import watch_heartbeat

    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, holder_name="claude-klabauter-ad", writer_session_id="w1",
        subscribed_peers=3,
    )
    rc = watch._cli(["--repo-root", str(tmp_path), "--status"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("ALIVE")


def test_cli_status_exits_two_on_a_repo_no_watch_ever_covered(tmp_path, capsys):
    # UNKNOWN gets its own code: a caller that reads 0 as "fine" must not read
    # "nobody ever looked" as fine, which is the collapse this flag exists for.
    rc = watch._cli(["--repo-root", str(tmp_path), "--status"])
    assert rc == 2
    assert capsys.readouterr().out.startswith("UNKNOWN")


def test_cli_status_exits_one_when_the_watch_stopped_ticking(tmp_path, capsys):
    import time as _time

    from coordinator_core.group_em import watch_heartbeat

    watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="group-em-1", declinations=[],
        interval_seconds=30.0, now_epoch=_time.time() - 3600, writer_session_id="w1",
    )
    rc = watch._cli(["--repo-root", str(tmp_path), "--status"])
    assert rc == 1
    assert capsys.readouterr().out.startswith("NOT RUNNING")


# --- C12: arming refuses when a fresh foreign holder already holds the watch
#
# `cross-repo/inbox/2026-08-31-doe-claude-em-watch-arm-refusal-yes-please.md`
# accepts our own proposal: a half handover -- crown and watcher both armed,
# each believing the other holds it -- is worse than neither. DISTINCT from
# C1 (`watch_heartbeat.stamp`'s own fresh-and-foreign decline): C1 stops a
# WRITE from clobbering a newer record once two watches are already both
# running; this stops the second ARM from ever starting.


def _stamp_holder(tmp_path, holder_session_id, writer_session_id, now_epoch, interval_seconds=30.0):
    from coordinator_core.group_em import watch_heartbeat

    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id=holder_session_id,
        declinations=[],
        interval_seconds=interval_seconds,
        writer_session_id=writer_session_id,
        now_epoch=now_epoch,
    )


def test_arming_refuses_against_a_fresh_foreign_holder(tmp_path):
    now = 1_000_000.0
    _stamp_holder(tmp_path, "foreign-holder", "foreign-writer", now_epoch=now)

    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [])
    ), mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        with pytest.raises(watch.WatchAlreadyHeldError) as excinfo:
            watch.main(
                str(tmp_path),
                caller_session_id="me",
                group_em_session_id="me",
                stream=io.StringIO(),
                sleep_fn=lambda _s: None,
                max_iterations=1,
                now_epoch=now + 5.0,
            )
    assert "foreign-holder" in str(excinfo.value)


def test_arming_names_the_holders_display_name_when_carried(tmp_path):
    from coordinator_core.group_em import watch_heartbeat

    now = 1_000_000.0
    watch_heartbeat.stamp(
        str(tmp_path),
        holder_session_id="foreign-holder",
        declinations=[],
        interval_seconds=30.0,
        writer_session_id="foreign-writer",
        holder_name="claude-klabauter-65",
        now_epoch=now,
    )

    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [])
    ), mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        with pytest.raises(watch.WatchAlreadyHeldError) as excinfo:
            watch.main(
                str(tmp_path),
                caller_session_id="me",
                group_em_session_id="me",
                stream=io.StringIO(),
                sleep_fn=lambda _s: None,
                max_iterations=1,
                now_epoch=now + 5.0,
            )
    assert "claude-klabauter-65" in str(excinfo.value)


def test_arming_proceeds_against_a_stale_foreign_holder(tmp_path):
    """The previous watcher is gone -- this is the case that must not be
    blocked, or a dead watch could never be replaced."""
    now = 1_000_000.0
    _stamp_holder(tmp_path, "foreign-holder", "foreign-writer", now_epoch=now, interval_seconds=1.0)

    armed_lines = []

    class _Stream:
        def write(self, text):
            if text.strip():
                armed_lines.append(text.strip())

        def flush(self):
            pass

    # `next_expected_by` is floored at 60s even for a 1s interval, so land
    # well past it.
    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [])
    ), mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        watch.main(
            str(tmp_path),
            caller_session_id="me",
            group_em_session_id="me",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
            now_epoch=now + 3600.0,
        )
    assert any(line.startswith("ARMED") for line in armed_lines)


def test_arming_proceeds_against_its_own_holder(tmp_path):
    """A tick this same crown wrote is not a foreign holder -- re-arming over
    its own record must not be blocked."""
    now = 1_000_000.0
    _stamp_holder(tmp_path, "me", "me", now_epoch=now)

    armed_lines = []

    class _Stream:
        def write(self, text):
            if text.strip():
                armed_lines.append(text.strip())

        def flush(self):
            pass

    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [])
    ), mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        watch.main(
            str(tmp_path),
            caller_session_id="me",
            group_em_session_id="me",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
            now_epoch=now + 5.0,
        )
    assert any(line.startswith("ARMED") for line in armed_lines)


def test_arming_proceeds_against_no_record_at_all(tmp_path):
    armed_lines = []

    class _Stream:
        def write(self, text):
            if text.strip():
                armed_lines.append(text.strip())

        def flush(self):
            pass

    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, [])
    ), mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        watch.main(
            str(tmp_path),
            caller_session_id="me",
            group_em_session_id="me",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
        )
    assert any(line.startswith("ARMED") for line in armed_lines)


def test_cli_exits_nonzero_and_names_the_holder_on_a_refused_arm(tmp_path, capsys):
    """Refusal is a non-zero exit with the holder named, not a silent no-op --
    an arm that quietly does nothing is indistinguishable from one that
    worked."""
    now = 1_000_000.0
    _stamp_holder(tmp_path, "foreign-holder", "foreign-writer", now_epoch=now)

    with mock.patch.object(watch.time, "time", return_value=now + 5.0):
        rc = watch._cli(
            [
                "--repo-root",
                str(tmp_path),
                "--caller-session-id",
                "me",
                "--group-em-session-id",
                "me",
                "--max-iterations",
                "1",
            ]
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert "foreign-holder" in err
    assert "group-em-watch:" in err


def test_cli_once_is_not_gated_by_the_arm_time_refusal(tmp_path):
    """`--once` is a stateless wake, never a second poller -- `watch_heartbeat.stamp`
    (C1) already declines its WRITE on a fresh foreign holder; this arm-time
    refusal is `main`'s alone and must not block a `--once` wake from running
    and reporting its own decline honestly."""
    now = 1_000_000.0
    _stamp_holder(tmp_path, "foreign-holder", "foreign-writer", now_epoch=now)

    with mock.patch.object(watch, "poll_once", return_value=({}, [], {})):
        rc = watch.tick_once(
            str(tmp_path),
            caller_session_id="me",
            group_em_session_id="me",
            stream=io.StringIO(),
            now=datetime.fromtimestamp(now + 5.0, tz=timezone.utc),
        )
    # tick_once itself never raises WatchAlreadyHeldError; it runs and its
    # own persistence path (C1) is what silently declines the write.
    assert rc == 0


def test_cli_status_watches_nothing_and_reads_no_roster(tmp_path, monkeypatch, capsys):
    # STATUS IS A READ. If it polled, asking "is my watch alive?" would cost the
    # box a roster enumeration per ask -- and a poll from a status call would
    # also stamp, making every ask answer ALIVE.
    def _refuse(*_a, **_k):
        raise AssertionError("--status must not enumerate the fleet")

    monkeypatch.setattr(watch, "poll_once", _refuse)
    assert watch._cli(["--repo-root", str(tmp_path), "--status"]) == 2
    capsys.readouterr()
