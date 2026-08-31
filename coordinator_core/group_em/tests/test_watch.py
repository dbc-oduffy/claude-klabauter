"""Tests for the standing watch (`coordinator_core/group_em/watch.py`, chunk C2).

Covers: `transitions`' pure not-parked->parked/parked->parked/spawn/exit
table, the obligation-names annotation (`no ledger` vs `none` vs named
records), the cooldown suppression that stops a re-flag of an answered peer,
the POLL-ERROR coverage path, and the measured poll-interval derivation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from coordinator_core.group_em import watch


REPO_ROOT = "/repo/root"


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
        result = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append)

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
        result = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={}, now=now, emit=emitted.append)

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
        result = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": False}, now=now, emit=emitted.append)

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
        result = watch.poll_once(REPO_ROOT, "caller-1", prev_parked={"peer-1": True}, now=now, emit=emitted.append)

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


def test_main_emits_poll_error_and_continues():
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
            REPO_ROOT,
            caller_session_id="caller-1",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=2,
        )

    assert any(line.startswith("ARMED denominator=3") for line in stream_lines)
    assert sum(1 for line in stream_lines if line.startswith("POLL-ERROR")) == 2


def test_main_arms_and_reports_measured_interval():
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
        watch, "poll_once", return_value={}
    ):
        watch.main(
            REPO_ROOT,
            caller_session_id="caller-1",
            stream=_Stream(),
            sleep_fn=lambda _s: None,
            max_iterations=1,
        )

    armed_line = stream_lines[0]
    assert armed_line.startswith("ARMED denominator=5 claude-klabauter peers, snapshot=2.0ms")
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
