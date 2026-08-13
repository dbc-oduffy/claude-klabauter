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
