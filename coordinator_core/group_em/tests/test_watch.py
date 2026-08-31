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
            watch, "_measure_snapshot_ms", return_value=(2.0, 7)
        ), mock.patch.object(watch, "poll_once", return_value=({}, [])):
            watch.main(
                str(root),
                caller_session_id="caller-1",
                stream=_Stream(),
                sleep_fn=lambda _s: None,
                max_iterations=1,
            )

        assert lines[0].startswith(f"ARMED peer_count=7 {name} peers, "), lines[0]
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
        watch, "_measure_snapshot_ms", return_value=(1.0, 3)
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
        watch, "_measure_snapshot_ms", return_value=(2.0, 5)
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
    assert armed_line.startswith(
        f"ARMED peer_count=5 {tmp_path.name} peers, snapshot=2.0ms"
    )
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
    monkeypatch.setattr(watch, "_measure_snapshot_ms", lambda repo_root: (4.6, 3))
    monkeypatch.setattr(watch, "_current_agents", lambda repo_root, sid: [])
    rc = watch._cli(
        ["--repo-root", str(tmp_path), "--caller-session-id", "sid-cli", "--max-iterations", "1"]
    )
    assert rc == 0
    assert "ARMED" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# two ids: the watching process is not always the crown
# (cross-repo/inbox/2026-08-31-doe-claude-em-fleet-watch-needs-engine-side-
# transition-events.md, question 1 -- DoE stands up a teammate to hold this
# watch so the crown's context stays free for adjudicating)
# ---------------------------------------------------------------------------


def test_roster_excludes_both_the_watcher_and_the_crown():
    """A teammate sitting in a `Monitor` poll presents exactly like a parked
    peer, and the crown is the recipient of every line -- neither belongs in
    the watched set."""
    agents = [
        {"sessionId": "watcher-1", "status": "idle", "cwd": REPO_ROOT},
        {"sessionId": "crown-1", "status": "idle", "cwd": REPO_ROOT},
        {"sessionId": "peer-1", "status": "idle", "cwd": REPO_ROOT},
    ]
    with mock.patch.object(watch.read_pass, "fetch_live_agents", return_value=agents):
        peers = watch._current_agents(REPO_ROOT, "watcher-1", "crown-1")
    assert [p["sessionId"] for p in peers] == ["peer-1"]


def test_cooldown_is_read_off_the_crowns_send_log_not_the_watchers():
    """The offer log is per-session on disk, so a watcher reading its own
    empty log would re-flag every peer the crown already answered."""
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
            crown_session_id="crown-1",
        )

    assert seen["caller"] == "crown-1"


def test_crown_session_id_defaults_to_the_calling_session():
    """The ordinary case -- the crown holds the poller itself -- keeps
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
    """A crown that arms this runnable and stops hand-stamping must not read
    to the rest of the fleet as a repo nobody is watching."""
    with mock.patch.object(
        watch, "_measure_snapshot_ms", return_value=(2.0, 5)
    ), mock.patch.object(
        watch, "poll_once", return_value=({}, [])
    ):
        watch.main(
            str(tmp_path),
            caller_session_id="watcher-1",
            crown_session_id="crown-1",
            stream=io.StringIO(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
        )

    with open(watch.watch_heartbeat.watch_path(str(tmp_path)), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["holder_session_id"] == "crown-1"
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
        watch.read_pass, "_transcript_activity_epoch", side_effect=AssertionError("re-read")
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
        "_transcript_activity_epoch",
        return_value=(now.timestamp() - 120.0, True),
    ), mock.patch.object(
        watch, "_stamped_age_seconds", return_value=None
    ), mock.patch.object(
        watch, "_obligation_summary", return_value="none"
    ):
        line = watch._parked_line(REPO_ROOT, "caller-1", "peer-1", verdict, REPO_ROOT, now)

    assert "transcript_idle=120s" in line
