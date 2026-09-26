
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

    def test_dirty_tree_case_c_disposition_carries_no_guidance(self):
        attribution = {"degraded": False, "mine": ["a.txt"], "residue_count": 1}
        points = ba._build_judgment_points("handoff", attribution)
        jp = next(p for p in points if p["id"] == "j-dirty-tree-case-c")
        assert jp["dispositions"] == [{"value": "mine", "resolves": ["d1"]}]


class TestNoHandMaintainedTrackerJudgmentPoint:
    """Negative spec (RENDERED-INDEX-NO-HAND-MAINTENANCE): `docs/project-tracker.md`
    is a retired rendered index, so NO judgment point may ask an EM to update,
    verify, or mirror it by hand -- for any `kind`, and regardless of whether a
    repo still carries the file. The substrate is `state/workstreams/`, queried
    directly. `j-tracker-hand-curated` and its two evidence/predicate helpers were
    deleted 2026-08-18; this asserts they stay deleted rather than returning under
    a narrower firing condition.
    """

    def test_no_judgment_point_names_the_tracker(self):
        for kind in ("handoff", "spinoff", "plan", "roadmap-baton"):
            points = ba._build_judgment_points(kind)
            assert not any(p["id"] == "j-tracker-hand-curated" for p in points)
            blob = " ".join(
                f"{p.get('question', '')} {p.get('evidence', '')}" for p in points
            ).lower()
            assert "project-tracker" not in blob, kind
