"""Tests for `_build_judgment_points` carrying per-option guidance
(AC4/AC5) via the shared `build_disposition` constructor (AC3).

`j-self-honesty`, `j-pm-auth`, and `j-continuation-vs-fork` carry non-empty
guidance on every disposition; `recommendation` stays `None` on all three
(structurally, via `build_untrusted_gate_judgment_point`'s no-parameter
signature -- unchanged by this addition).

Spec backlink: pln-build-disposition-carries-per-399055
"""

from __future__ import annotations

import coordinator_core.baton_assemble as ba


class TestSelfHonestyGuidance:
    def test_proceed_carries_non_empty_guidance(self):
        points = ba._build_judgment_points("handoff")
        jp = next(p for p in points if p["id"] == "j-self-honesty")
        (disposition,) = jp["dispositions"]
        assert disposition["value"] == "proceed"
        assert isinstance(disposition["guidance"], str)
        assert disposition["guidance"].strip() != ""

    def test_recommendation_stays_none(self):
        points = ba._build_judgment_points("handoff")
        jp = next(p for p in points if p["id"] == "j-self-honesty")
        assert jp["recommendation"] is None


class TestPmAuthGuidance:
    def test_authorized_carries_non_empty_guidance(self):
        points = ba._build_judgment_points("handoff")
        jp = next(p for p in points if p["id"] == "j-pm-auth")
        (disposition,) = jp["dispositions"]
        assert disposition["value"] == "authorized"
        assert isinstance(disposition["guidance"], str)
        assert disposition["guidance"].strip() != ""

    def test_recommendation_stays_none(self):
        points = ba._build_judgment_points("handoff")
        jp = next(p for p in points if p["id"] == "j-pm-auth")
        assert jp["recommendation"] is None


class TestContinuationVsForkGuidance:
    def test_every_disposition_carries_non_empty_guidance(self):
        points = ba._build_judgment_points("handoff")
        jp = next(p for p in points if p["id"] == "j-continuation-vs-fork")
        assert {d["value"] for d in jp["dispositions"]} == {"continue", "excise"}
        for disposition in jp["dispositions"]:
            assert isinstance(disposition["guidance"], str)
            assert disposition["guidance"].strip() != ""

    def test_recommendation_stays_none(self):
        points = ba._build_judgment_points("handoff")
        jp = next(p for p in points if p["id"] == "j-continuation-vs-fork")
        assert jp["recommendation"] is None

    def test_not_emitted_for_kind_spinoff(self):
        points = ba._build_judgment_points("spinoff")
        assert not any(p["id"] == "j-continuation-vs-fork" for p in points)


class TestNoGuidanceDispositionsUnaffected:
    """AC3 routes j-dirty-tree-case-c/j-tracker-hand-curated through
    `build_disposition` too, but Out-of-scope explicitly withholds guidance
    text for them this pass -- their shape must stay exactly as before."""

    def test_dirty_tree_case_c_disposition_carries_no_guidance(self):
        attribution = {"degraded": False, "mine": ["a.txt"], "residue_count": 1}
        points = ba._build_judgment_points("handoff", attribution)
        jp = next(p for p in points if p["id"] == "j-dirty-tree-case-c")
        assert jp["dispositions"] == [{"value": "mine", "resolves": ["d1"]}]

    def test_tracker_hand_curated_dispositions_carry_no_guidance(self, tmp_path):
        points = ba._build_judgment_points(
            "handoff", tracker_hand_curated=True, root=tmp_path
        )
        jp = next(p for p in points if p["id"] == "j-tracker-hand-curated")
        assert jp["dispositions"] == [
            {"value": "recorded", "resolves": ["d1"]},
            {"value": "nothing-to-record", "resolves": ["d1"]},
        ]


class TestTrackerHandCuratedSpinoff:
    """2026-08-14 fix: `j-tracker-hand-curated` was gated on `kind ==
    "handoff"` only, so a `/spinoff` never received the tracker obligation
    as a judgment point -- it survived only as SKILL.md prose slated for a
    size-reduction cut. Widened to also cover `kind == "spinoff"`, with its
    own correctly-framed question/evidence (marking the fork in the SOURCE
    session's tracker, not recording this session's own progress).

    Spec backlink: break-class fix, j-tracker-hand-curated spinoff gate.
    """

    def test_emitted_for_kind_spinoff(self, tmp_path):
        points = ba._build_judgment_points(
            "spinoff", tracker_hand_curated=True, root=tmp_path
        )
        assert any(p["id"] == "j-tracker-hand-curated" for p in points)

    def test_still_emitted_for_kind_handoff(self, tmp_path):
        points = ba._build_judgment_points(
            "handoff", tracker_hand_curated=True, root=tmp_path
        )
        assert any(p["id"] == "j-tracker-hand-curated" for p in points)

    def test_spinoff_evidence_names_source_session_not_this_session(self, tmp_path):
        points = ba._build_judgment_points(
            "spinoff", tracker_hand_curated=True, root=tmp_path
        )
        jp = next(p for p in points if p["id"] == "j-tracker-hand-curated")
        assert "source session" in jp["evidence"].lower()
        assert "this session progressed" not in jp["evidence"].lower()

    def test_not_emitted_for_other_kinds_when_not_hand_curated(self, tmp_path):
        points = ba._build_judgment_points(
            "spinoff", tracker_hand_curated=False, root=tmp_path
        )
        assert not any(p["id"] == "j-tracker-hand-curated" for p in points)
