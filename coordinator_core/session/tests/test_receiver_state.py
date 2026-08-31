"""
coordinator_core.session.tests.test_receiver_state — tests for the receiver-state
verdict ladder, structural reduction, CPU cursor, and sibling-file writer.

Fixture transcripts are synthesized IN-TEST (never a real session transcript read from
disk — the plan's C5 body forbids it). Default tier (not cadence/pending_fix/designed_red).

Spec backlink: docs/plans/2026-08-14-receiver-state-sensor.md § C5
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.session import receiver_state as rs


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _assistant_tool_use(tool_name: str, *, timestamp: str = "2026-08-14T00:00:00Z") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": tool_name, "id": "t1", "input": {}}],
            },
        }
    )


def _assistant_end_turn(*, prose: str = "") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-08-14T00:00:00Z",
            "message": {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": prose}],
            },
        }
    )


def _system_away_summary() -> str:
    return json.dumps({"type": "system", "subtype": "away_summary", "timestamp": "2026-08-14T00:00:00Z"})


def _system_stop_hook_summary() -> str:
    return json.dumps({"type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-08-14T00:00:00Z"})


def _user_with_result(prose: str = "") -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": "2026-08-14T00:00:00Z",
            "message": {"content": [{"type": "tool_result", "content": prose}]},
        }
    )


def _user_turn_starting() -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": "2026-08-14T00:00:00Z",
            "message": {"content": [{"type": "text", "text": "go"}]},
        }
    )


def _control_line(kind: str) -> str:
    # Untimestamped control lines the walk-back must skip past.
    return json.dumps({"type": kind})


def _sidechain_line() -> str:
    return json.dumps({"type": "assistant", "isSidechain": True, "message": {"stop_reason": "end_turn"}})


def _unmodelled_line(line_type: str = "assistant", subtype: str = "image") -> str:
    # A state-bearing TYPE (assistant/user) whose shape the ladder still doesn't
    # recognise (no stop_reason matching any arm) — this is what actually reaches
    # `_classify_one`'s final UNKNOWN:unmodelled branch under the allow-list walk-back.
    # A non-state-bearing type (e.g. "attachment") is walked PAST by
    # `_select_last_substantive_line` and never reaches `_classify_one` at all — see
    # `test_non_state_bearing_type_is_walked_past_not_recorded_as_unmodelled`.
    return json.dumps({"type": line_type, "subtype": subtype, "timestamp": "2026-08-14T00:00:00Z"})


def _write_transcript(tmp_path: Path, lines: list[str]) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _reduce(tmp_path: Path, lines: list[str]) -> list:
    path = _write_transcript(tmp_path, lines)
    reduced, _unparseable, _cap = rs.reduce_transcript_tail(path)
    return reduced


# ---------------------------------------------------------------------------
# Ladder arms
# ---------------------------------------------------------------------------


class TestLadderArms:
    def test_away_summary_pauses_away(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_system_away_summary()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PAUSED"
        assert v.reason == "away"

    def test_ask_user_question_pauses_asking_human(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_assistant_tool_use("AskUserQuestion")])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PAUSED"
        assert v.reason == "asking-human"

    def test_exit_plan_mode_pauses_asking_human(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_assistant_tool_use("ExitPlanMode")])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PAUSED"
        assert v.reason == "asking-human"

    def test_other_tool_in_flight_producing_within_grace(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_assistant_tool_use("Bash")])
        v = rs.classify(
            reduced, now_epoch=1000.0, transcript_activity_epoch=999.0, delegation_evidence=False
        )
        assert v.verdict == "PRODUCING"
        assert v.reason == "tool-in-flight"

    def test_other_tool_in_flight_downgrades_past_grace(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_assistant_tool_use("Bash")])
        v = rs.classify(
            reduced,
            now_epoch=1000.0,
            transcript_activity_epoch=1000.0 - rs._TOOL_UNANSWERED_GRACE_SECONDS - 1,
            delegation_evidence=False,
        )
        assert v.verdict == "PAUSED"
        assert v.reason.startswith("tool-unanswered")

    def test_stop_hook_summary_pauses_turn_ended(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_system_stop_hook_summary()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PAUSED"
        assert v.reason == "turn-ended"

    def test_assistant_end_turn_pauses_turn_ended(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_assistant_end_turn()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PAUSED"
        assert v.reason == "turn-ended"

    def test_user_with_result_producing_mid_turn(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_user_with_result()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PRODUCING"
        assert v.reason == "mid-turn"

    def test_user_without_result_producing_turn_starting(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_user_turn_starting()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PRODUCING"
        assert v.reason == "turn-starting"

    def test_isSidechain_filtered(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_assistant_end_turn(), _sidechain_line()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        # Sidechain line must be filtered so the ladder falls back to the real
        # last substantive (non-sidechain) line: assistant end_turn.
        assert v.verdict == "PAUSED"
        assert v.reason == "turn-ended"

    def test_control_line_walkback(self, tmp_path: Path) -> None:
        reduced = _reduce(
            tmp_path,
            [_system_away_summary(), _control_line("mode"), _control_line("last-prompt")],
        )
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PAUSED"
        assert v.reason == "away"


class TestUnknownAndDelegationOverride:
    def test_unmodelled_line_yields_unknown_and_records_type(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_unmodelled_line("assistant", "image")])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "UNKNOWN"
        assert v.unmodelled_type == "assistant"
        assert v.unmodelled_subtype == "image"

    def test_non_state_bearing_type_is_walked_past_not_recorded_as_unmodelled(
        self, tmp_path: Path
    ) -> None:
        """Regression for state/audits/2026-08-30-group-em-classifier-blindness.md:
        `attachment` (and the other non-state-bearing types it names) must be walked
        PAST to the real last state-bearing line, never classified as UNKNOWN:unmodelled
        in its own right."""
        reduced = _reduce(
            tmp_path, [_system_stop_hook_summary(), _unmodelled_line("attachment", "image")]
        )
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "PAUSED"
        assert v.reason == "turn-ended"

    def test_window_all_non_state_bearing_yields_unknown_with_new_reason(
        self, tmp_path: Path
    ) -> None:
        reduced = _reduce(tmp_path, [_unmodelled_line("attachment", "image"), _control_line("atis-latch")])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "UNKNOWN"
        assert "none state-bearing" in v.reason
        assert "isSidechain" not in v.reason

    def test_unknown_plus_live_delegation_evidence_overrides_to_producing_delegated(
        self, tmp_path: Path
    ) -> None:
        """AC3 regression: the spike's real observed bug — a session with 1 live
        subagent and a 3.5s-old transcript returned UNKNOWN. Our fix (module docstring
        (c), step 7's "or UNKNOWN") must rescue this case, not only a PAUSED one."""
        reduced = _reduce(tmp_path, [_unmodelled_line()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=True)
        assert v.verdict == "PRODUCING"
        assert "delegated" in v.reason

    def test_paused_plus_live_delegation_evidence_overrides_to_producing_delegated(
        self, tmp_path: Path
    ) -> None:
        reduced = _reduce(tmp_path, [_system_away_summary()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=True)
        assert v.verdict == "PRODUCING"
        assert "delegated" in v.reason

    def test_producing_verdict_not_overridden_by_delegation(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_user_with_result()])
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=True)
        assert v.verdict == "PRODUCING"
        assert v.reason == "mid-turn"


class TestDelegationEvidenceFromSidecar:
    """state/audits/2026-08-30-group-em-classifier-blindness.md's root-cause finding:
    `delegation_evidence` was ALWAYS False in production because nothing ever
    computed it — these tests cover the producer this dispatch adds
    (`delegation_evidence_from_sidecar`) across its FOUR distinct outcomes: present,
    absent, no-transcript-path (case (a): no information at all — bare `None`), and
    listing-errored (case (b): genuinely ambiguous — `DELEGATION_UNRESOLVED`). The
    two `None`-shaped causes were originally collapsed together and had to be split
    apart after review; see `merge_delegation_evidence`'s docstring for why they
    must stay distinct."""

    @staticmethod
    def _sidecar_dir(tmp_path: Path) -> tuple[Path, str]:
        """Build <tmp>/session.jsonl + <tmp>/session/subagents/ and return
        (subagents_dir, transcript_path) — the exact shape
        `_subagents_dir_for` derives from a real transcript path."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        subagents_dir = tmp_path / "session" / "subagents"
        subagents_dir.mkdir(parents=True)
        return subagents_dir, str(transcript)

    def test_present_recent_mtime_is_true(self, tmp_path: Path) -> None:
        subagents_dir, transcript_path = self._sidecar_dir(tmp_path)
        agent_file = subagents_dir / "agent-abc123.jsonl"
        agent_file.write_text("{}\n", encoding="utf-8")
        now = os.path.getmtime(agent_file) + 5.0
        result = rs.delegation_evidence_from_sidecar(transcript_path, now_epoch=now)
        assert result is True

    def test_present_stale_mtime_is_false(self, tmp_path: Path) -> None:
        subagents_dir, transcript_path = self._sidecar_dir(tmp_path)
        agent_file = subagents_dir / "agent-abc123.jsonl"
        agent_file.write_text("{}\n", encoding="utf-8")
        stale_now = os.path.getmtime(agent_file) + rs._DELEGATION_ACTIVITY_GRACE_SECONDS + 1.0
        result = rs.delegation_evidence_from_sidecar(transcript_path, now_epoch=stale_now)
        assert result is False

    def test_no_subagents_directory_is_false(self, tmp_path: Path) -> None:
        transcript = tmp_path / "solo-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        result = rs.delegation_evidence_from_sidecar(str(transcript), now_epoch=0.0)
        assert result is False

    def test_no_transcript_path_is_bare_none_no_information(self) -> None:
        """Case (a): no transcript_path at all is NO INFORMATION, distinct from a
        failed check. Must be a bare `None`, never `DELEGATION_UNRESOLVED` and never
        `False` — this is what an UNKNOWN ladder verdict already means, and
        `merge_delegation_evidence` must not override on it (see that function's
        test class below)."""
        assert rs.delegation_evidence_from_sidecar(None, now_epoch=0.0) is None
        assert rs.delegation_evidence_from_sidecar("", now_epoch=0.0) is None

    def test_unreadable_directory_is_delegation_unresolved(self, tmp_path: Path, monkeypatch) -> None:
        """Case (b): a transcript path WAS supplied and DID resolve to a candidate
        directory, but listing it failed for a reason other than "does not exist"
        (e.g. a permission error) — genuinely ambiguous, must be the
        `DELEGATION_UNRESOLVED` sentinel, never a bare `None` and never `False`."""
        subagents_dir, transcript_path = self._sidecar_dir(tmp_path)

        def _boom(_path):
            raise PermissionError("simulated")

        monkeypatch.setattr(rs.os, "scandir", _boom)
        result = rs.delegation_evidence_from_sidecar(transcript_path, now_epoch=0.0)
        assert result is rs.DELEGATION_UNRESOLVED
        assert result is not None

    def test_non_jsonl_entries_ignored(self, tmp_path: Path) -> None:
        subagents_dir, transcript_path = self._sidecar_dir(tmp_path)
        meta_file = subagents_dir / "agent-abc123.meta.json"
        meta_file.write_text("{}\n", encoding="utf-8")
        now = os.path.getmtime(meta_file) + 5.0
        result = rs.delegation_evidence_from_sidecar(transcript_path, now_epoch=now)
        assert result is False


class TestMergeDelegationEvidence:
    def test_payload_true_wins_regardless_of_sidecar(self) -> None:
        assert rs.merge_delegation_evidence(True, False) is True
        assert rs.merge_delegation_evidence(True, None) is True
        assert rs.merge_delegation_evidence(True, rs.DELEGATION_UNRESOLVED) is True

    def test_sidecar_true_wins_when_payload_false(self) -> None:
        assert rs.merge_delegation_evidence(False, True) is True

    def test_delegation_unresolved_fails_toward_true(self) -> None:
        """Case (b) — a check was attempted and genuinely could not be answered —
        must fail toward PRODUCING, never collapse to the same outcome as a
        definitive negative."""
        assert rs.merge_delegation_evidence(False, rs.DELEGATION_UNRESOLVED) is True

    def test_no_transcript_path_does_not_override(self) -> None:
        """Case (a) — regression coverage for the design error caught in review: a
        bare `None` sidecar signal (no transcript path, i.e. NO information at all)
        must NOT be treated the same as `DELEGATION_UNRESOLVED`. Overriding here
        would manufacture positive delegation evidence out of an absence of
        information and destroy the UNKNOWN verdict the ladder already produces —
        UNKNOWN is never a nudge candidate on its own, so this buys zero additional
        protection against the spurious-nudge defect."""
        assert rs.merge_delegation_evidence(False, None) is False

    def test_both_false_is_false(self) -> None:
        assert rs.merge_delegation_evidence(False, False) is False


# ---------------------------------------------------------------------------
# AC9 — privacy: the reduction must be structurally incapable of holding prose
# ---------------------------------------------------------------------------


class TestPrivacyReduction:
    _SENTINEL = "ZZZ_DISTINCTIVE_PROSE_SENTINEL_MUST_NOT_ESCAPE_ZZZ"

    def test_sentinel_absent_from_reduced_lines(self, tmp_path: Path) -> None:
        reduced = _reduce(
            tmp_path,
            [
                _assistant_end_turn(prose=self._SENTINEL),
                _user_with_result(prose=self._SENTINEL),
            ],
        )
        for line in reduced:
            for value in dataclasses_values(line):
                assert self._SENTINEL not in str(value)

    def test_sentinel_absent_from_reduced_line_dataclass_fields(self) -> None:
        # Structural assertion: _ReducedLine has no field shaped to hold prose at all
        # (no "text"/"content"/"message" field name), independent of any fixture.
        field_names = {f.name for f in rs.dataclasses.fields(rs._ReducedLine)}
        assert "text" not in field_names
        assert "content" not in field_names
        assert "message" not in field_names

    def test_sentinel_absent_from_written_state_file(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        reduced = _reduce(
            tmp_path,
            [_assistant_end_turn(prose=self._SENTINEL)],
        )
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        ok = rs.write_receiver_state(
            "sid-privacy", verdict=v, cpu_cursor=None, stamp_iso="2026-08-14T00:00:00Z", cwd=str(tmp_path)
        )
        assert ok
        written = rs.read_receiver_state("sid-privacy", str(tmp_path))
        assert self._SENTINEL not in json.dumps(written)


def dataclasses_values(obj):
    import dataclasses as _dc

    return [getattr(obj, f.name) for f in _dc.fields(obj)]


def _fake_session_dir(monkeypatch, tmp_path: Path):
    """Point session.core's session-dir resolution AND its constructor at a
    tmp_path-rooted fake sessions dir, so the sibling-file writer test never
    touches the real repo's .git/coordinator-sessions/.

    Both must be patched. `write_receiver_state` calls `core.ensure_session`
    (the one constructor) and then fails closed on `os.path.isdir`; patching
    only `session_dir` left the constructor resolving the REAL hub from the
    process cwd and minting session directories into the live tree, which is
    precisely what this helper exists to prevent (and what `conftest`'s live
    session-hub litter guard catches). The fake constructor writes a
    `meta.json` too, because that is the real one's postcondition -- a fake
    that produced a record-less directory would bake into the fixture the very
    state `ensure_session` exists to make impossible.
    """
    import json as _json

    from coordinator_core.session import core as session_core

    fake_base = tmp_path / "coordinator-sessions"

    def _fake_session_dir(sid: str, cwd=None) -> str:
        return str(fake_base / sid)

    def _fake_ensure_session(sid: str, cwd=None, **_kwargs) -> str:
        d = fake_base / sid
        d.mkdir(parents=True, exist_ok=True)
        meta = d / "meta.json"
        if not meta.is_file():
            meta.write_text(
                _json.dumps({"session_id": sid, "goal": ""}, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        return str(d)

    monkeypatch.setattr(session_core, "session_dir", _fake_session_dir)
    monkeypatch.setattr(session_core, "ensure_session", _fake_ensure_session)


# ---------------------------------------------------------------------------
# AC6 — no sleep anywhere; structural, not wall-clock
# ---------------------------------------------------------------------------


class TestNoSleep:
    def test_module_source_contains_no_time_sleep(self) -> None:
        import inspect

        source = inspect.getsource(rs)
        assert "time.sleep" not in source
        assert "asyncio.sleep" not in source

    def test_classify_completes_well_inside_caller_budget(self, tmp_path: Path) -> None:
        reduced = _reduce(tmp_path, [_assistant_end_turn()])
        started = time.monotonic()
        rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0  # well inside the 5s caller budget


# ---------------------------------------------------------------------------
# AC12 — fail-soft
# ---------------------------------------------------------------------------


class TestFailSoft:
    def test_missing_transcript_yields_unknown(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "does-not-exist.jsonl")
        reduced, _unparseable, _cap = rs.reduce_transcript_tail(missing)
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "UNKNOWN"

    def test_empty_file_yields_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        reduced, _unparseable, _cap = rs.reduce_transcript_tail(str(path))
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "UNKNOWN"

    def test_unreadable_directory_path_yields_unknown(self, tmp_path: Path) -> None:
        # A directory, not a file — open() raises OSError (IsADirectoryError), which
        # _read_tail_lines must catch, not propagate.
        reduced, _unparseable, _cap = rs.reduce_transcript_tail(str(tmp_path))
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "UNKNOWN"

    def test_truncated_final_line_does_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "truncated.jsonl"
        path.write_text('{"type": "assistant", "message": {"stop', encoding="utf-8")
        reduced, unparseable, _cap = rs.reduce_transcript_tail(str(path))
        assert unparseable is True
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "UNKNOWN"

    def test_garbage_json_does_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "garbage.jsonl"
        path.write_text("not json at all\n{{{{\n", encoding="utf-8")
        reduced, unparseable, _cap = rs.reduce_transcript_tail(str(path))
        assert unparseable is True
        v = rs.classify(reduced, now_epoch=0.0, transcript_activity_epoch=None, delegation_evidence=False)
        assert v.verdict == "UNKNOWN"


# ---------------------------------------------------------------------------
# CPU cursor — tiebreak-only gating (AC7, AC8 underived-constant discipline)
# ---------------------------------------------------------------------------


class TestCpuCursorTiebreakOnly:
    def test_cpu_leg_disabled_by_default(self) -> None:
        assert rs._CPU_LEG_ENABLED is False
        assert rs._CPU_FLOOR_UNDERIVED is None

    def test_cpu_tiebreak_never_fires_while_disabled(self) -> None:
        assert rs.resolve_cpu_tiebreak(0.99) is None
        assert rs.resolve_cpu_tiebreak(0.0) is None

    def test_resolve_verdict_never_overturns_confident_paused(self) -> None:
        confident = rs.Verdict("PAUSED", "turn-ended")
        resolved = rs.resolve_verdict(confident, cpu_rate=0.99)
        assert resolved is confident

    def test_resolve_verdict_never_overturns_confident_producing(self) -> None:
        confident = rs.Verdict("PRODUCING", "mid-turn")
        resolved = rs.resolve_verdict(confident, cpu_rate=0.0)
        assert resolved is confident

    def test_resolve_verdict_leaves_unknown_unresolved_while_leg_disabled(self) -> None:
        unknown = rs.Verdict("UNKNOWN", "unmodelled")
        resolved = rs.resolve_verdict(unknown, cpu_rate=0.99)
        assert resolved.verdict == "UNKNOWN"

    def test_cpu_delta_rate_no_previous_cursor(self) -> None:
        current = rs.CpuCursor(cpu_seconds=1.0, wall_clock_epoch=100.0)
        assert rs.cpu_delta_rate(None, current) is None

    def test_cpu_delta_rate_non_positive_wall_delta(self) -> None:
        previous = rs.CpuCursor(cpu_seconds=1.0, wall_clock_epoch=100.0)
        current = rs.CpuCursor(cpu_seconds=2.0, wall_clock_epoch=100.0)
        assert rs.cpu_delta_rate(previous, current) is None

    def test_cpu_delta_rate_normal_case(self) -> None:
        previous = rs.CpuCursor(cpu_seconds=1.0, wall_clock_epoch=100.0)
        current = rs.CpuCursor(cpu_seconds=3.0, wall_clock_epoch=104.0)
        assert rs.cpu_delta_rate(previous, current) == pytest.approx(0.5)

    def test_cpu_delta_rate_regression_returns_none(self) -> None:
        # A CPU-seconds regression (process restart reusing a pid) must not yield a
        # negative rate.
        previous = rs.CpuCursor(cpu_seconds=5.0, wall_clock_epoch=100.0)
        current = rs.CpuCursor(cpu_seconds=1.0, wall_clock_epoch=104.0)
        assert rs.cpu_delta_rate(previous, current) is None


# ---------------------------------------------------------------------------
# Sibling-file writer — never meta.json, never state/
# ---------------------------------------------------------------------------


class TestSiblingFileWriter:
    def test_writes_under_coordinator_sessions_sibling_never_meta_json(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        v = rs.Verdict("PAUSED", "turn-ended")
        ok = rs.write_receiver_state(
            "sid-1", verdict=v, cpu_cursor=None, stamp_iso="2026-08-14T00:00:00Z", cwd=str(tmp_path)
        )
        assert ok
        sibling = tmp_path / "coordinator-sessions" / "sid-1" / "receiver-state.json"
        assert sibling.is_file()
        assert not (tmp_path / "state").exists()

        # The property is that receiver-state is a SIBLING FILE, never a field
        # inside the session record -- `meta.json` is a contended artifact and
        # a per-turn verdict has no business in it.
        #
        # This used to be spelled `assert not meta.exists()`, which stopped
        # being a statement about THIS writer on 2026-08-26: `ensure_session`
        # is the session directory's one constructor and creates the record
        # together with the directory, so a `meta.json` beside the sibling is
        # now the CORRECT postcondition, not a leak. Asserting its absence
        # would pin a record-less session directory -- exactly the state the
        # constructor exists to make impossible. Pin the containment instead.
        meta = tmp_path / "coordinator-sessions" / "sid-1" / "meta.json"
        if meta.exists():
            record = json.loads(meta.read_text(encoding="utf-8"))
            leaked = {"verdict", "reason", "cpu_cursor", "stamp", "receiver_state"} & set(record)
            assert not leaked, (
                f"receiver-state field(s) {sorted(leaked)} leaked into meta.json -- "
                "this writer owns a sibling file, never the session record"
            )

    def test_written_record_shape(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        v = rs.Verdict("UNKNOWN", "unmodelled line type='x'", unmodelled_type="x", unmodelled_subtype="y")
        cursor = rs.CpuCursor(cpu_seconds=1.5, wall_clock_epoch=100.0)
        ok = rs.write_receiver_state(
            "sid-2", verdict=v, cpu_cursor=cursor, stamp_iso="2026-08-14T00:00:00Z", cwd=str(tmp_path)
        )
        assert ok
        record = rs.read_receiver_state("sid-2", str(tmp_path))
        assert record["verdict"] == "UNKNOWN"
        assert record["unmodelled_type"] == "x"
        assert record["unmodelled_subtype"] == "y"
        assert record["cpu_cursor"]["cpu_seconds"] == 1.5

    def test_unsafe_sid_rejected(self, tmp_path: Path, monkeypatch) -> None:
        _fake_session_dir(monkeypatch, tmp_path)
        v = rs.Verdict("PAUSED", "turn-ended")
        ok = rs.write_receiver_state(
            "../escape", verdict=v, cpu_cursor=None, stamp_iso="x", cwd=str(tmp_path)
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_check_registration_quad_covers_receiver_state_sensor(self) -> None:
        """Offline check (AC3-style): pass all five tables explicitly rather than
        letting check_registration_quad() trigger the full ops-tree discovery walk —
        that walk imports every module under coordinator_core/ops/ (including ones
        unrelated to this op, and currently broken by an unrelated concurrent edit on
        this shared tree), which is not this test's concern. Only registration of
        THIS op across the five surfaces is under test here."""
        from coordinator_core.authz.classification import OP_CLASSIFICATION
        from coordinator_core.authz.registration_quad import check_registration_quad
        from coordinator_core.op_scopes import _OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP
        from coordinator_core.ops import _EAGER_OP_MODULES
        from coordinator_core.ipc import _REGISTRY

        # Import the op module directly to guarantee registration for this
        # process, independent of the eager-import ordering of the full suite.
        import coordinator_core.hooks.receiver_state_sensor  # noqa: F401

        assert "hooks.receiver_state_sensor" in _REGISTRY

        registry = {"hooks.receiver_state_sensor": _REGISTRY["hooks.receiver_state_sensor"]}
        eager_modules = frozenset(module_path for module_path, _note in _EAGER_OP_MODULES)
        violations = check_registration_quad(
            registry=registry,
            classification=OP_CLASSIFICATION,
            scope=_OP_KEY_SCOPE,
            module_map=OP_MODULE_MAP,
            eager_modules=eager_modules,
        )
        assert violations == [], f"hooks.receiver_state_sensor missing surfaces: {violations}"


class TestActivityEpochFromReduced:
    """The shared transcript clock, used by both planes that classify a peer
    (`hooks/receiver_state_sensor.py` and `group_em.read_pass`). File mtime is
    not this number: the harness rewrites a stopped session's transcript with
    untimestamped bookkeeping rows, moving mtime without the session acting."""

    def _line(self, timestamp: str):
        return rs._ReducedLine(
            type="assistant",
            subtype="",
            timestamp=timestamp,
            stop_reason="end_turn",
            pending_background_agent_count=0,
            tool_names=(),
            tool_result_markers=(),
            parse_ok=True,
        )

    def test_returns_the_newest_parseable_timestamp(self):
        lines = [self._line("2026-08-31T15:00:00Z"), self._line("2026-08-31T15:40:48Z")]
        assert rs.activity_epoch_from_reduced(lines) == 1788190848.0

    def test_scans_past_untimestamped_bookkeeping_rows(self):
        """The defect's exact shape: real record, then rows the harness wrote."""
        lines = [self._line("2026-08-31T15:40:48Z"), self._line(""), self._line("")]
        assert rs.activity_epoch_from_reduced(lines) == 1788190848.0

    def test_none_when_nothing_carries_a_timestamp(self):
        """`None` is the caller's signal to fall back to mtime as an upper
        bound -- never an age of zero, and never "has not moved"."""
        assert rs.activity_epoch_from_reduced([self._line("")]) is None
        assert rs.activity_epoch_from_reduced([]) is None

    def test_malformed_timestamps_are_skipped_not_guessed_at(self):
        lines = [self._line("2026-08-31T15:40:48Z"), self._line("not-a-timestamp")]
        assert rs.activity_epoch_from_reduced(lines) == 1788190848.0
