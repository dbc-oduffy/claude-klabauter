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

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from coordinator_core.group_em import read_pass
from coordinator_core.session import receiver_state


REPO_ROOT = "/repo/root"


def _agent(session_id="peer-1", status="idle", cwd=REPO_ROOT):
    return {"sessionId": session_id, "status": status, "cwd": cwd}


# ---------------------------------------------------------------------------
# reader / fallback split
# ---------------------------------------------------------------------------


def test_reader_leg_used_when_record_present():
    # `stamped_at` is fresh here -- a real record always carries it (see
    # `write_receiver_state`); this fixture is updated to that real shape by
    # defect B's fail-closed idle-side staleness guard, not weakened by it.
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


# ---------------------------------------------------------------------------
# defect B -- idle-side stale-snapshot guard (mid-work peer must never be a
# candidate on either leg)
# ---------------------------------------------------------------------------


def test_stale_paused_snapshot_with_idle_status_not_a_candidate():
    # Real shape from state/audits/2026-08-30-group-em-cooldown-vs-candidacy-
    # window.md: peer 30342983 was offered mid-turn on a reader snapshot
    # stamped ~210s earlier (above the p50=108s pin) while the harness read
    # `idle`. This must never be a candidate.
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
    """A parked peer is not disqualified for having sat still.

    `receiver-state.json` is written at turn end, so a genuinely parked
    session's snapshot only ages. Judging on age alone permanently hid every
    peer idle longer than the p50 pin -- exactly the peers a Group EM exists
    to surface. Evidence of stillness (transcript untouched since the
    snapshot) reinstates candidacy however old the snapshot is.
    """
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
    """The mid-turn peer defect B exists for is still never a candidate.

    Audit peer 30342983 was mid-turn on a ~210s-old snapshot while the
    harness read `idle`. A transcript newer than the snapshot is that peer:
    it has acted since, so the snapshot is genuinely misleading.
    """
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
    """No evidence of stillness is never read AS stillness."""
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


# ---------------------------------------------------------------------------
# defect 4 -- a frozen PRODUCING reader verdict must not silently hide a
# stopped peer from the roster (state/dispatch-briefs/2026-08-31-the-group-
# em-tick-carries-standing-obligations/C7.md)
# ---------------------------------------------------------------------------


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
    """No evidence the frozen verdict has gone stale -- must not be flagged."""
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
    """`None` (unreadable transcript / unparseable stamp) leaves the frozen
    verdict standing -- never manufactured into a stale/unclassifiable claim."""
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


# ---------------------------------------------------------------------------
# classifier collapse (overengineering review finding 1): the fallback leg's
# idle arm now calls receiver_state.reduce_transcript_tail + receiver_state.classify
# directly, rather than carrying a second bounded reader/classifier. These
# tests exercise `classify_fallback_status` against real `_ReducedLine`s
# produced by `receiver_state.reduce_transcript_tail` over a real tmp
# transcript file -- no private receiver_state helper is touched directly.
# ---------------------------------------------------------------------------


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
    # The atis-latch gap the local classifier used to carry: a burst of
    # control lines pushed the real last-substantive line out of the narrower
    # 40-line window. receiver_state's wider 64-line/256KB window plus its
    # allow-list walk-back (skip past unrecognised types rather than stopping
    # on them) recovers the real line -- verifying the collapse actually
    # closes the gap rather than assuming it.
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
        "idle", reduced, now_epoch=0.0, transcript_mtime_epoch=0.0
    )
    assert state == read_pass.STATE_PRODUCING


def test_classify_fallback_status_user_line_still_producing(tmp_path):
    reduced = _reduced_lines_for(tmp_path, [{"type": "user"}])
    state, _reason = read_pass.classify_fallback_status("idle", reduced, now_epoch=0.0)
    assert state == read_pass.STATE_PRODUCING


# ---------------------------------------------------------------------------
# PRODUCING peers are never candidates, on either leg
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# in-engine roster enumeration, no subprocess spawn
# ---------------------------------------------------------------------------


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
    fake_build_roster.assert_called_once_with(repo_root=REPO_ROOT)
    assert agents == [{"sessionId": "peer-1", "status": "busy", "cwd": REPO_ROOT}]


def test_fetch_live_agents_empty_when_build_roster_empty():
    # `build_roster`'s own contract degrades an internal failure to `[]`
    # rather than raising (default `raise_on_failure=False`) -- this is the
    # observable shape `fetch_live_agents` sees for that degrade, since it
    # adds no extra try/except of its own on top of `build_roster`'s.
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
