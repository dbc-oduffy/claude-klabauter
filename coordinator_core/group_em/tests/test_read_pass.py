"""Tests for the in-plane Group EM read pass (`coordinator_core/group_em/read_pass.py`).

Covers: the reader/fallback split (reader record present vs. `None`), the
fallback leg's delegation to `receiver_state.reduce_transcript_tail` +
`receiver_state.classify` (the classifier collapse, overengineering review
finding 1) including that a wide-window atis-latch burst no longer masks the
real last-substantive line, that a `PRODUCING` peer is excluded from the
candidate roster on both the reader leg and the fallback leg, the idle-side
stale-snapshot guard (defect B), and `stop_reason`-aware turn-ended detection
on the fallback tail (defect C).
"""

from __future__ import annotations

import types
import json

import pytest
from datetime import datetime, timedelta, timezone
from unittest import mock

from coordinator_core.group_em import read_pass
from coordinator_core.session import receiver_state


REPO_ROOT = "/repo/root"


def _agent(session_id="peer-1", status="idle", cwd=REPO_ROOT):
    return {"sessionId": session_id, "status": status, "cwd": cwd}


def test_reader_leg_used_when_record_present():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={
            "verdict": "PAUSED",
            "reason": "turn-ended",
            "stamped_at": now.isoformat(),
        },
    ):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["source"] == "reader"
    assert verdict["state"] == "PAUSED"
    assert verdict["candidate"] is True


def test_fallback_leg_used_when_reader_returns_none():
    with mock.patch.object(read_pass, "read_receiver_state", return_value=None):
        verdict = read_pass.classify_peer(
            REPO_ROOT,
            _agent(status="busy"),
        )
    assert verdict["source"] == "fallback"
    assert verdict["state"] == read_pass.STATE_PRODUCING
    assert verdict["candidate"] is False


def test_reader_paused_contradicted_by_live_busy_status():
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended"},
    ):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="busy"))
    assert verdict["source"] == "reader"
    assert verdict["reason"] == "live-busy-contradicts-paused"
    assert verdict["candidate"] is False


def test_stale_paused_snapshot_with_idle_status_not_a_candidate():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=210)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended", "stamped_at": stamped_at},
    ):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["source"] == "reader"
    assert verdict["reason"] == "stale-snapshot-contradicts-paused"
    assert verdict["candidate"] is False


def test_fresh_paused_snapshot_with_idle_status_is_still_a_candidate():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended", "stamped_at": stamped_at},
    ):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["candidate"] is True
    assert verdict["reason"] == "turn-ended"


def test_stale_snapshot_but_transcript_still_reinstates_the_candidate():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended", "stamped_at": stamped_at},
    ), mock.patch.object(read_pass, "_transcript_moved_since", return_value=False):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["candidate"] is True
    assert verdict["reason"] == "turn-ended"


def test_stale_snapshot_with_moved_transcript_stays_out_defect_b_preserved():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=210)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended", "stamped_at": stamped_at},
    ), mock.patch.object(read_pass, "_transcript_moved_since", return_value=True):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["candidate"] is False
    assert verdict["reason"] == "stale-snapshot-contradicts-paused"


def test_unreadable_transcript_leaves_the_age_verdict_standing():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=210)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended", "stamped_at": stamped_at},
    ), mock.patch.object(read_pass, "_transcript_moved_since", return_value=None):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["candidate"] is False


def test_indeterminate_staleness_fails_closed_not_a_candidate():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended", "stamped_at": "not-a-timestamp"},
    ):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["reason"] == "stale-snapshot-unresolved"
    assert verdict["candidate"] is False


def test_missing_stamped_at_fails_closed_not_a_candidate():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended"},
    ):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["reason"] == "stale-snapshot-unresolved"
    assert verdict["candidate"] is False


# defect 4 -- a frozen PRODUCING reader verdict must not silently hide a


def test_stale_producing_snapshot_resolves_unknown_and_unclassifiable():
    """`write_receiver_state` has one writer (the Stop hook); a session that
    never takes another turn leaves this verdict frozen. Transcript growth
    after `stamped_at` is positive evidence the frozen PRODUCING verdict no
    longer describes the peer -- must resolve UNKNOWN, never PAUSED, and be
    flagged `unclassifiable` rather than silently dropped."""
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={
            "verdict": "PRODUCING",
            "reason": "delegated (overrides PAUSED: turn-ended)",
            "stamped_at": stamped_at,
        },
    ), mock.patch.object(read_pass, "_transcript_moved_since", return_value=True):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["state"] == read_pass.STATE_UNKNOWN
    assert verdict["state"] != read_pass.STATE_PAUSED
    assert verdict["candidate"] is False
    assert verdict["unclassifiable"] is True
    assert "stale-producing" in verdict["reason"]


def test_stale_producing_snapshot_reaches_the_roster_payload_though_not_a_candidate():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={
            "verdict": "PRODUCING",
            "reason": "delegated (overrides PAUSED: turn-ended)",
            "stamped_at": stamped_at,
        },
    ), mock.patch.object(read_pass, "_transcript_moved_since", return_value=True):
        roster = read_pass.build_candidate_roster(
            REPO_ROOT,
            agents=[_agent(session_id="peer-1", status="idle")],
            caller_session_id_value="caller",
            now=now,
        )
    assert len(roster) == 1
    assert roster[0]["unclassifiable"] is True
    assert roster[0]["candidate"] is False


def test_producing_snapshot_not_moved_since_stays_producing_not_unclassifiable():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    stamped_at = (now - timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={
            "verdict": "PRODUCING",
            "reason": "delegated (overrides PAUSED: turn-ended)",
            "stamped_at": stamped_at,
        },
    ), mock.patch.object(read_pass, "_transcript_moved_since", return_value=False):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["state"] == read_pass.STATE_PRODUCING
    assert verdict["unclassifiable"] is False
    assert verdict["candidate"] is False


def test_producing_snapshot_indeterminate_movement_stays_producing_not_unclassifiable():
    now = datetime(2026, 8, 30, 18, 39, 22, tzinfo=timezone.utc)
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={
            "verdict": "PRODUCING",
            "reason": "delegated (overrides PAUSED: turn-ended)",
            "stamped_at": "not-a-timestamp",
        },
    ), mock.patch.object(read_pass, "_transcript_moved_since", return_value=None):
        verdict = read_pass.classify_peer(REPO_ROOT, _agent(status="idle"), now=now)
    assert verdict["state"] == read_pass.STATE_PRODUCING
    assert verdict["unclassifiable"] is False


def _write_transcript(tmp_path, records):
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return str(path)


def _reduced_lines_for(tmp_path, records):
    path = _write_transcript(tmp_path, records)
    reduced, _any_unparseable, _cap_reached = receiver_state.reduce_transcript_tail(path)
    return reduced


def test_classify_fallback_status_unknown_on_unrecognised_type_only(tmp_path):
    reduced = _reduced_lines_for(tmp_path, [{"type": "atis-latch", "atis": ""}])
    state, _reason = read_pass.classify_fallback_status(
        "idle", reduced, now_epoch=0.0
    )
    assert state == read_pass.STATE_UNKNOWN


def test_classify_fallback_status_unknown_on_empty(tmp_path):
    state, _reason = read_pass.classify_fallback_status("idle", [], now_epoch=0.0)
    assert state == read_pass.STATE_UNKNOWN


def test_classify_fallback_status_atis_latch_burst_does_not_mask_real_line(tmp_path):
    records = [{"type": "system", "subtype": "turn_duration"}]
    records += [{"type": "atis-latch", "atis": ""} for _ in range(35)]
    reduced = _reduced_lines_for(tmp_path, records)
    state, reason = read_pass.classify_fallback_status("idle", reduced, now_epoch=0.0)
    assert state == read_pass.STATE_PAUSED
    assert "turn-ended" in reason


def test_classify_fallback_status_end_turn_is_paused_without_system_line(tmp_path):
    reduced = _reduced_lines_for(
        tmp_path, [{"type": "assistant", "message": {"stop_reason": "end_turn"}}]
    )
    state, _reason = read_pass.classify_fallback_status("idle", reduced, now_epoch=0.0)
    assert state == read_pass.STATE_PAUSED


def test_classify_fallback_status_tool_use_stop_reason_still_producing(tmp_path):
    reduced = _reduced_lines_for(
        tmp_path, [{"type": "assistant", "message": {"stop_reason": "tool_use"}}]
    )
    state, _reason = read_pass.classify_fallback_status(
        "idle", reduced, now_epoch=0.0, transcript_activity_epoch=0.0
    )
    assert state == read_pass.STATE_PRODUCING


def test_classify_fallback_status_user_line_still_producing(tmp_path):
    reduced = _reduced_lines_for(tmp_path, [{"type": "user"}])
    state, _reason = read_pass.classify_fallback_status("idle", reduced, now_epoch=0.0)
    assert state == read_pass.STATE_PRODUCING


# PRODUCING peers are never candidates, on either leg


def test_producing_peer_excluded_on_reader_leg():
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PRODUCING", "reason": "mid-turn"},
    ):
        roster = read_pass.build_candidate_roster(
            REPO_ROOT,
            agents=[_agent(session_id="peer-1", status="busy")],
            caller_session_id_value="caller",
        )
    assert roster == []


def test_producing_peer_excluded_on_fallback_leg_via_busy_status():
    with mock.patch.object(read_pass, "read_receiver_state", return_value=None):
        roster = read_pass.build_candidate_roster(
            REPO_ROOT,
            agents=[_agent(session_id="peer-1", status="busy")],
            caller_session_id_value="caller",
        )
    assert roster == []


def test_producing_peer_excluded_on_fallback_leg_via_live_tail(tmp_path):
    reduced = _reduced_lines_for(tmp_path, [{"type": "assistant"}])

    def fake_read_tail(session_id, cwd):
        return reduced

    with mock.patch.object(read_pass, "read_receiver_state", return_value=None):
        roster = read_pass.build_candidate_roster(
            REPO_ROOT,
            agents=[_agent(session_id="peer-1", status="idle")],
            caller_session_id_value="caller",
            read_tail=fake_read_tail,
        )
    assert roster == []


def test_paused_fallback_peer_is_a_candidate(tmp_path):
    reduced = _reduced_lines_for(tmp_path, [{"type": "system", "subtype": "turn_duration"}])

    def fake_read_tail(session_id, cwd):
        return reduced

    with mock.patch.object(read_pass, "read_receiver_state", return_value=None):
        roster = read_pass.build_candidate_roster(
            REPO_ROOT,
            agents=[_agent(session_id="peer-1", status="idle")],
            caller_session_id_value="caller",
            read_tail=fake_read_tail,
        )
    assert len(roster) == 1
    assert roster[0]["state"] == read_pass.STATE_PAUSED
    assert roster[0]["candidate"] is True


def test_caller_excluded_from_own_roster():
    with mock.patch.object(
        read_pass,
        "read_receiver_state",
        return_value={"verdict": "PAUSED", "reason": "turn-ended"},
    ):
        roster = read_pass.build_candidate_roster(
            REPO_ROOT,
            agents=[_agent(session_id="caller", status="idle")],
            caller_session_id_value="caller",
        )
    assert roster == []


def _peer_row(session_id="peer-1", status="idle", cwd=REPO_ROOT, is_self=False):
    from coordinator_core.session.peer_roster import PeerRow

    return PeerRow(
        session_id=session_id,
        address=None,
        name=None,
        ref=None,
        cwd=cwd,
        status=status,
        running_seconds=0.0,
        is_self=is_self,
        self_determination="resolved",
        messaging_available=False,
    )


def test_fetch_live_agents_sources_peer_roster_not_a_subprocess():
    with mock.patch.object(
        read_pass.peer_roster,
        "build_roster",
        return_value=[_peer_row(session_id="peer-1", status="busy")],
    ) as fake_build_roster:
        agents = read_pass.fetch_live_agents(REPO_ROOT)
    # BOTH REFUSALS DEFAULT OFF, asserted rather than omitted. The two flags
    fake_build_roster.assert_called_once_with(
        repo_root=REPO_ROOT, raise_on_failure=False, raise_on_empty_snapshot=False
    )
    assert agents == [
        {"sessionId": "peer-1", "status": "busy", "cwd": REPO_ROOT, "name": None}
    ]


def test_fetch_live_agents_empty_when_build_roster_empty():
    with mock.patch.object(read_pass.peer_roster, "build_roster", return_value=[]):
        assert read_pass.fetch_live_agents(REPO_ROOT) == []


def test_build_candidate_roster_uses_fetch_live_agents_when_agents_omitted():
    with mock.patch.object(
        read_pass, "fetch_live_agents", return_value=[]
    ) as fake_fetch:
        roster = read_pass.build_candidate_roster(
            REPO_ROOT, caller_session_id_value="caller"
        )
    fake_fetch.assert_called_once_with(REPO_ROOT)
    assert roster == []


def test_module_imports_no_subprocess_and_defines_no_command_constant():
    import sys

    assert "subprocess" not in vars(read_pass)
    assert not hasattr(read_pass, "_CLAUDE_AGENTS_CMD")
    module = sys.modules[read_pass.__name__]
    assert "subprocess" not in getattr(module, "__dict__", {})


def _write_clock_transcript(tmp_path, session_id, cwd, records, mtime_epoch=None):
    import os

    projects = tmp_path / "projects" / read_pass._PATH_SEP_RE.sub("-", cwd)
    projects.mkdir(parents=True, exist_ok=True)
    path = projects / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    if mtime_epoch is not None:
        os.utime(path, (mtime_epoch, mtime_epoch))
    return str(path)


def _patch_transcript_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        read_pass,
        "_transcript_path_for",
        lambda session_id, cwd: str(
            tmp_path / "projects" / read_pass._PATH_SEP_RE.sub("-", cwd) / f"{session_id}.jsonl"
        ),
    )


def test_activity_epoch_ignores_an_mtime_pushed_forward_by_bookkeeping(tmp_path, monkeypatch):
    _patch_transcript_root(monkeypatch, tmp_path)
    last_real = datetime(2026, 8, 31, 15, 40, 48, tzinfo=timezone.utc)
    _write_clock_transcript(
        tmp_path,
        "peer-stalled",
        REPO_ROOT,
        [
            {"type": "assistant", "timestamp": last_real.isoformat().replace("+00:00", "Z")},
            {"type": "last-prompt"},
            {"type": "ai-title"},
            {"type": "cost-state"},
        ],
        mtime_epoch=last_real.timestamp() + 420.0,
    )

    epoch, trusted = read_pass.transcript_activity_epoch("peer-stalled", REPO_ROOT)

    assert trusted is True
    assert epoch == last_real.timestamp()


def test_activity_epoch_falls_back_to_mtime_untrusted_when_nothing_is_timestamped(
    tmp_path, monkeypatch
):
    _patch_transcript_root(monkeypatch, tmp_path)
    _write_clock_transcript(
        tmp_path, "peer-no-stamps", REPO_ROOT, [{"type": "cost-state"}], mtime_epoch=1000.0
    )

    epoch, trusted = read_pass.transcript_activity_epoch("peer-no-stamps", REPO_ROOT)

    assert trusted is False
    assert epoch == 1000.0


def test_activity_epoch_is_none_when_the_transcript_is_absent(tmp_path, monkeypatch):
    _patch_transcript_root(monkeypatch, tmp_path)
    assert read_pass.transcript_activity_epoch("peer-missing", REPO_ROOT) == (None, False)


def test_moved_since_is_not_answered_from_an_untrusted_clock(tmp_path, monkeypatch):
    _patch_transcript_root(monkeypatch, tmp_path)
    stamp = datetime(2026, 8, 31, 15, 0, 0, tzinfo=timezone.utc)
    _write_clock_transcript(
        tmp_path,
        "peer-bookkeeping-only",
        REPO_ROOT,
        [{"type": "cost-state"}],
        mtime_epoch=stamp.timestamp() + 600.0,
    )

    assert read_pass._transcript_moved_since("peer-bookkeeping-only", REPO_ROOT, stamp) is None


def test_moved_since_still_answers_true_on_a_real_later_record(tmp_path, monkeypatch):
    _patch_transcript_root(monkeypatch, tmp_path)
    stamp = datetime(2026, 8, 31, 15, 0, 0, tzinfo=timezone.utc)
    later = (stamp + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")
    _write_clock_transcript(
        tmp_path, "peer-really-moved", REPO_ROOT, [{"type": "assistant", "timestamp": later}]
    )

    assert read_pass._transcript_moved_since("peer-really-moved", REPO_ROOT, stamp) is True


def test_moved_since_answers_false_from_an_untrusted_clock_that_never_passed_the_stamp(
    tmp_path, monkeypatch
):
    _patch_transcript_root(monkeypatch, tmp_path)
    stamp = datetime(2026, 8, 31, 15, 0, 0, tzinfo=timezone.utc)
    _write_clock_transcript(
        tmp_path,
        "peer-still",
        REPO_ROOT,
        [{"type": "cost-state"}],
        mtime_epoch=stamp.timestamp() - 120.0,
    )

    assert read_pass._transcript_moved_since("peer-still", REPO_ROOT, stamp) is False


def test_classify_peer_threads_its_activity_epoch_onto_the_verdict(tmp_path, monkeypatch):
    _patch_transcript_root(monkeypatch, tmp_path)
    last_real = datetime(2026, 8, 31, 15, 40, 48, tzinfo=timezone.utc)
    _write_clock_transcript(
        tmp_path,
        "peer-threaded",
        REPO_ROOT,
        [
            {
                "type": "assistant",
                "timestamp": last_real.isoformat().replace("+00:00", "Z"),
                "message": {"stop_reason": "end_turn", "content": []},
            }
        ],
    )
    monkeypatch.setattr(read_pass, "read_receiver_state", lambda sid, root: None)

    verdict = read_pass.classify_peer(REPO_ROOT, _agent(session_id="peer-threaded"))

    assert verdict["activity_epoch"] == last_real.timestamp()


def test_the_projection_carries_the_peer_name_it_used_to_drop(monkeypatch):
    row = types.SimpleNamespace(
        session_id="peer-1", status="idle", cwd=REPO_ROOT, name="claude-klabauter-65"
    )
    monkeypatch.setattr(read_pass.peer_roster, "build_roster", lambda **_: [row])

    projected = read_pass.fetch_live_agents(REPO_ROOT)

    assert projected == [
        {"sessionId": "peer-1", "status": "idle", "cwd": REPO_ROOT, "name": "claude-klabauter-65"}
    ]


def test_build_roster_is_exported_under_the_name_the_doctrine_plane_calls():
    assert callable(getattr(read_pass, "build_roster", None))


def test_the_candidate_shortlist_is_a_subset_of_the_full_roster(monkeypatch):
    agents = [
        {"sessionId": "caller", "cwd": "/repo", "name": "self", "status": "busy"},
        {"sessionId": "peer-a", "cwd": "/repo", "name": "a", "status": "idle"},
        {"sessionId": "peer-b", "cwd": "/repo", "name": "b", "status": "busy"},
    ]
    full = read_pass.build_roster(
        "/repo", agents=agents, caller_session_id_value="caller"
    )
    shortlist = read_pass.build_candidate_roster(
        "/repo", agents=agents, caller_session_id_value="caller"
    )
    assert len(full) == 2
    full_ids = {v["session_id"] for v in full}
    assert {v["session_id"] for v in shortlist} <= full_ids


class TestIsAdmitted:

    def test_candidate_is_admitted(self):
        verdict = {"candidate": True, "unclassifiable": False, "contradicted": False}
        assert read_pass.is_admitted(verdict) is True

    def test_unclassifiable_is_admitted(self):
        verdict = {"candidate": False, "unclassifiable": True, "contradicted": False}
        assert read_pass.is_admitted(verdict) is True

    def test_contradicted_is_admitted(self):
        verdict = {"candidate": False, "unclassifiable": False, "contradicted": True}
        assert read_pass.is_admitted(verdict) is True

    def test_none_of_the_three_is_not_admitted(self):
        verdict = {"candidate": False, "unclassifiable": False, "contradicted": False}
        assert read_pass.is_admitted(verdict) is False

    def test_build_candidate_roster_calls_through_is_admitted(self, monkeypatch):
        agents = [{"sessionId": "peer-a", "cwd": "/repo", "status": "idle"}]
        monkeypatch.setattr(read_pass, "is_admitted", lambda verdict: True)
        admitted_all = read_pass.build_candidate_roster(
            "/repo", agents=agents, caller_session_id_value=None
        )
        monkeypatch.setattr(read_pass, "is_admitted", lambda verdict: False)
        admitted_none = read_pass.build_candidate_roster(
            "/repo", agents=agents, caller_session_id_value=None
        )
        assert len(admitted_all) == 1
        assert admitted_none == []


class TestFallbackStatusFallThroughNamesWhatItSaw:

    @pytest.mark.parametrize("status", [None, ""], ids=["none", "empty"])
    def test_absent_status_says_absent(self, status):
        state, reason = read_pass.classify_fallback_status(status, reduced_lines=[])
        assert (state, reason) == (read_pass.STATE_UNKNOWN, "status-absent")

    def test_unrecognized_status_names_the_value_it_could_not_map(self):
        state, reason = read_pass.classify_fallback_status("wedged", reduced_lines=[])
        assert state == read_pass.STATE_UNKNOWN
        assert reason == "unrecognized-status:wedged"

    def test_case_variant_of_a_known_status_is_not_silently_absent(self):
        state, reason = read_pass.classify_fallback_status("BUSY", reduced_lines=[])
        assert (state, reason) == (read_pass.STATE_UNKNOWN, "unrecognized-status:BUSY")

    def test_known_arms_are_untouched(self):
        assert read_pass.classify_fallback_status("busy", reduced_lines=[]) == (
            read_pass.STATE_PRODUCING,
            "status-busy",
        )


class TestShellIsAnExecutingStatus:

    def test_shell_classifies_as_producing_and_names_itself(self):
        assert read_pass.classify_fallback_status("shell", reduced_lines=[]) == (
            read_pass.STATE_PRODUCING,
            "status-shell",
        )

    def test_the_reason_still_distinguishes_which_executing_status_it_saw(self):
        _, busy = read_pass.classify_fallback_status("busy", reduced_lines=[])
        _, shell = read_pass.classify_fallback_status("shell", reduced_lines=[])
        assert busy != shell

    def test_an_unknown_status_is_still_unrecognized_not_swept_into_executing(self):
        assert read_pass.classify_fallback_status("wedged", reduced_lines=[]) == (
            read_pass.STATE_UNKNOWN,
            "unrecognized-status:wedged",
        )

    def test_a_live_shell_contradicts_a_stored_paused_verdict_as_busy_does(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            read_pass,
            "read_receiver_state",
            lambda session_id, repo_root: {
                "verdict": read_pass.STATE_PAUSED,
                "reason": "turn-ended",
                "stamped_at": "2026-09-02T20:00:00Z",
            },
        )
        verdicts = {
            status: read_pass.classify_peer(
                str(tmp_path),
                {"sessionId": "s1", "status": status, "cwd": str(tmp_path)},
                read_tail=lambda session_id, cwd: [],
            )
            for status in ("busy", "shell")
        }
        assert verdicts["shell"]["contradicted"] == verdicts["busy"]["contradicted"]
        assert verdicts["shell"]["candidate"] == verdicts["busy"]["candidate"]
