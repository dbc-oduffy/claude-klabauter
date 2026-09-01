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

from datetime import datetime, timezone
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
        ), mock.patch.object(watch, "poll_once", return_value=({}, [])):
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


def test_exit_emits_nothing():
    prev = {"peer-1": True}
    cur: dict[str, bool] = {}
    assert watch.transitions(prev, cur) == []


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


def _agent(session_id="peer-1", status="idle", cwd=REPO_ROOT):
    return {"sessionId": session_id, "status": status, "cwd": cwd}


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
        result, _declinations = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append)

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
        result, _declinations = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={}, now=now, emit=emitted.append)

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
        result, _declinations = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append)

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
        result, _declinations = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": True}, now=now, emit=emitted.append)

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
        watch, "poll_once", return_value=({}, [])
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
        _parked, declinations = watch.poll_once(
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
        watch, "poll_once", return_value=({}, [])
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
        line = watch._parked_line(REPO_ROOT, "caller-1", "peer-1", verdict, REPO_ROOT, now)

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
        line = watch._parked_line(REPO_ROOT, "caller-1", "peer-1", verdict, REPO_ROOT, now)

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
    """Drive one `tick_once` over a single peer with a fixed verdict."""
    watch.save_prev_parked(str(repo_root), prev_on_disk)
    now = now or datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
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
    with mock.patch.object(watch, "poll_once", return_value=({}, [])):
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
        watch, "poll_once", return_value=({"p1": True, "p2": False, "p3": False}, [])
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
    ) as measured, mock.patch.object(watch, "poll_once", return_value=({}, [])):
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
    )
    with mock.patch.object(watch, "poll_once", return_value=({}, [])):
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
    )
    watch.watch_heartbeat.stamp(
        str(tmp_path), holder_session_id="Group-EM-2", declinations=[], interval_seconds=5.0
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
    assert "not an absolute path" in err
    assert "resolves against" in err


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
    ), mock.patch.object(watch, "poll_once", return_value=({}, [])):
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
