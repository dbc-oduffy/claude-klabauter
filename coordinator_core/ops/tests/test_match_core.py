"""
coordinator_core.ops.tests.test_match_core — Unit tests for the generic ranking core.

Coverage:
  (a) empty items → returns []
  (b) single item → returns one ranked result with {id, title, score}
  (c) score formula — 0.7 * ratio + 0.3 * overlap, rounded to 4 decimals
  (d) ranking order — higher score first; tie-break by id ASCENDING
  (e) score range — always 0.0 ≤ score ≤ 1.0
  (f) empty query → safe (no raise; scores near 0.0)
  (g) exact-match query → score = 1.0
  (h) items list is not mutated by rank_candidates
  (i) resolve_candidate — auto-resolution is score-load-bearing, not
      candidate-COUNT-bearing: a many-item directory with one clear match
      auto-resolves (the regression the "arithmetic auto-resolution" defect
      this helper replaces was named for -- this must have failed against
      the old ``len(items) == 1`` branch it replaces); a query that matches
      nothing (below the score floor) degrades with reason BELOW_THRESHOLD;
      two near-identical top scorers degrade with reason TOO_CLOSE; a LONE
      candidate that fails the floor still degrades (never auto-resolved
      just for being alone); empty items degrades with reason NO_CANDIDATES.

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C1
"""

from __future__ import annotations

import difflib

from coordinator_core.ops.match_core import (
    AUTO_RESOLVE_MIN_GAP,
    AUTO_RESOLVE_MIN_SCORE,
    ResolutionReason,
    rank_candidates,
    resolve_candidate,
)


def _expected_score(query: str, haystack: str) -> float:
    query_lower = query.lower()
    ratio_score = difflib.SequenceMatcher(None, query_lower, haystack).ratio()
    query_tokens = set(query_lower.split())
    haystack_tokens = set(haystack.split())
    overlap = len(query_tokens & haystack_tokens) / max(len(query_tokens), 1)
    return round(0.7 * ratio_score + 0.3 * overlap, 4)


class TestRankCandidatesCore:

    def test_empty_items_returns_empty(self):
        assert rank_candidates("any query", []) == []

    def test_empty_query_is_safe(self):
        items = [{"id": "x", "title": "X", "text": "some haystack text"}]
        result = rank_candidates("", items)
        assert len(result) == 1
        assert 0.0 <= result[0]["score"] <= 1.0

    def test_single_item_result_shape(self):
        items = [{"id": "g-1", "title": "Goal One", "text": "improve legibility of the okr system"}]
        result = rank_candidates("legibility", items)
        assert len(result) == 1
        entry = result[0]
        assert entry["id"] == "g-1"
        assert entry["title"] == "Goal One"
        assert isinstance(entry["score"], float)
        assert 0.0 <= entry["score"] <= 1.0

    def test_score_formula_exact_values(self):
        query = "okr tracking legibility"
        haystack = "okr legibility system make okr tracking legible across the engineering team"
        items = [{"id": "g-leg", "title": "OKR Legibility", "text": haystack}]
        result = rank_candidates(query, items)
        assert result[0]["score"] == _expected_score(query, haystack)

    def test_score_formula_snapshot(self):
        h_unrelated = "unrelated goal completely different topic about infrastructure"
        h_legibility = "okr legibility system make okr tracking legible across the engineering team"
        query = "okr tracking legibility"

        items = [
            {"id": "unrelated", "title": "Unrelated Goal", "text": h_unrelated},
            {"id": "okr-legibility", "title": "OKR Legibility System", "text": h_legibility},
        ]
        result = rank_candidates(query, items)

        assert result[0]["id"] == "okr-legibility"
        assert result[1]["id"] == "unrelated"
        assert result[0]["score"] == 0.6
        assert result[1]["score"] == 0.1318

    def test_ranking_order_highest_first(self):
        items = [
            {"id": "low", "title": "Low Match", "text": "completely unrelated xyz"},
            {"id": "high", "title": "High Match", "text": "reduce rendering latency below 100ms"},
        ]
        result = rank_candidates("reduce rendering latency", items)
        assert result[0]["id"] == "high"
        assert result[0]["score"] >= result[1]["score"]

    def test_tie_break_by_id_ascending(self):
        """Equal-score items are tie-broken by id ASCENDING."""
        identical_text = "apple banana cherry"
        items = [
            {"id": "zzz-last", "title": "Z Item", "text": identical_text},
            {"id": "aaa-first", "title": "A Item", "text": identical_text},
        ]
        result = rank_candidates("apple", items)
        assert result[0]["id"] == "aaa-first"
        assert result[1]["id"] == "zzz-last"

    def test_score_range_always_zero_to_one(self):
        items = [
            {"id": "a", "title": "Alpha", "text": "alpha beta gamma"},
            {"id": "b", "title": "Beta", "text": ""},
            {"id": "c", "title": "Gamma", "text": "completely different delta epsilon"},
        ]
        result = rank_candidates("alpha beta", items)
        for entry in result:
            assert 0.0 <= entry["score"] <= 1.0

    def test_exact_match_score_is_one(self):
        query = "exact match phrase"
        items = [{"id": "exact", "title": "Exact", "text": query}]
        result = rank_candidates(query, items)
        assert result[0]["score"] == 1.0

    def test_items_not_mutated(self):
        original = [
            {"id": "g-1", "title": "Goal One", "text": "some objective text"},
            {"id": "g-2", "title": "Goal Two", "text": "another goal"},
        ]
        original_copy = [dict(item) for item in original]
        rank_candidates("objective", original)
        assert original == original_copy

    def test_kr_text_contributes_via_haystack(self):
        h_with_kr = "generic goal improve overall performance reduce perceptual rendering latency below 100ms"
        h_control = "unrelated control expand marketing reach into new demographics"
        items = [
            {"id": "g-kr", "title": "Generic Goal", "text": h_with_kr},
            {"id": "g-control", "title": "Unrelated Control", "text": h_control},
        ]
        result = rank_candidates("reduce perceptual rendering latency below 100ms", items)
        assert result[0]["id"] == "g-kr"
        assert result[0]["score"] == _expected_score(
            "reduce perceptual rendering latency below 100ms", h_with_kr
        )


def _many_plan_items(n: int = 50) -> list:
    items = [
        {
            "id": f"pln-filler-{i:03d}",
            "title": f"Filler Plan Number {i}",
            "text": f"filler plan number {i} unrelated infrastructure cleanup",
        }
        for i in range(n)
    ]
    items.append(
        {
            "id": "pln-target",
            "title": "Retire Legacy Auth Middleware",
            "text": "retire legacy auth middleware",
        }
    )
    return items


class TestResolveCandidateCore:

    def test_many_items_clear_match_auto_resolves(self):
        items = _many_plan_items(50)
        result = resolve_candidate("Retire Legacy Auth Middleware", items)
        assert result["resolved_id"] == "pln-target"
        assert result["reason"] is None
        assert len(result["ranked"]) == 51

    def test_timestamp_slug_query_against_many_plans_is_below_threshold(self):
        """A timestamp-slug query (the shape of stamp mode's old buggy
        match_text default) against a many-plan directory degrades with
        reason BELOW_THRESHOLD -- nothing scored well enough, because the
        slug shares almost nothing with any prose title."""
        items = _many_plan_items(50)
        result = resolve_candidate("2026-08-02_141130_some-slug", items)
        assert result["resolved_id"] is None
        assert result["reason"] == ResolutionReason.BELOW_THRESHOLD
        assert result["ranked"][0]["score"] < AUTO_RESOLVE_MIN_SCORE

    def test_two_near_identical_titles_are_too_close(self):
        """Two candidates that both score well and sit within min_gap of
        each other degrade with reason TOO_CLOSE -- a genuine tie, distinct
        from an uninformative query."""
        items = [
            {"id": "pln-a", "title": "Retire Legacy Auth Middleware", "text": "retire legacy auth middleware"},
            {"id": "pln-b", "title": "Retire Legacy Auth Middlewares", "text": "retire legacy auth middlewares"},
        ]
        result = resolve_candidate("Retire Legacy Auth Middleware", items)
        assert result["resolved_id"] is None
        assert result["reason"] == ResolutionReason.TOO_CLOSE
        top, runner_up = result["ranked"][0], result["ranked"][1]
        assert top["score"] - runner_up["score"] < AUTO_RESOLVE_MIN_GAP

    def test_lone_candidate_below_floor_still_degrades(self):
        items = [{"id": "pln-only", "title": "Retire Legacy Auth Middleware", "text": "retire legacy auth middleware"}]
        result = resolve_candidate("2026-08-02_141130_some-slug", items)
        assert result["resolved_id"] is None
        assert result["reason"] == ResolutionReason.BELOW_THRESHOLD
        assert len(result["ranked"]) == 1

    def test_lone_candidate_above_floor_resolves(self):
        items = [{"id": "pln-only", "title": "Retire Legacy Auth Middleware", "text": "retire legacy auth middleware"}]
        result = resolve_candidate("Retire Legacy Auth Middleware", items)
        assert result["resolved_id"] == "pln-only"
        assert result["reason"] is None

    def test_empty_items_is_no_candidates(self):
        """Empty items → reason NO_CANDIDATES (graceful-absent, distinct from
        an uninformative query scored against a non-empty corpus)."""
        result = resolve_candidate("anything", [])
        assert result["resolved_id"] is None
        assert result["reason"] == ResolutionReason.NO_CANDIDATES
        assert result["ranked"] == []

    def test_thresholds_are_keyword_overridable(self):
        items = [{"id": "pln-only", "title": "Something", "text": "something"}]
        result = resolve_candidate("totally unrelated query", items, min_score=0.0, min_gap=0.0)
        assert result["resolved_id"] == "pln-only"
        assert result["reason"] is None


    def test_score_exactly_at_floor_still_resolves(self):
        items = [{"id": "pln-only", "title": "Retire Legacy Auth Middleware", "text": "retire legacy auth middleware"}]
        ranked = rank_candidates("Retire Legacy Auth Middleware", items)
        exact_score = ranked[0]["score"]
        result = resolve_candidate(
            "Retire Legacy Auth Middleware", items, min_score=exact_score, min_gap=0.0
        )
        assert result["resolved_id"] == "pln-only"
        assert result["reason"] is None

    def test_gap_exactly_at_min_gap_still_resolves(self):
        items = [
            {"id": "pln-a", "title": "Retire Legacy Auth Middleware", "text": "retire legacy auth middleware"},
            {"id": "pln-b", "title": "Retire Legacy Auth Middlewares", "text": "retire legacy auth middlewares"},
        ]
        ranked = rank_candidates("Retire Legacy Auth Middleware", items)
        exact_gap = ranked[0]["score"] - ranked[1]["score"]
        result = resolve_candidate(
            "Retire Legacy Auth Middleware", items, min_score=0.0, min_gap=exact_gap
        )
        assert result["resolved_id"] == ranked[0]["id"]
        assert result["reason"] is None
