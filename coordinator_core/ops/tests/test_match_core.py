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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_score(query: str, haystack: str) -> float:
    """Reference implementation of the scoring formula — mirrors match_core.py verbatim."""
    query_lower = query.lower()
    ratio_score = difflib.SequenceMatcher(None, query_lower, haystack).ratio()
    query_tokens = set(query_lower.split())
    haystack_tokens = set(haystack.split())
    overlap = len(query_tokens & haystack_tokens) / max(len(query_tokens), 1)
    return round(0.7 * ratio_score + 0.3 * overlap, 4)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRankCandidatesCore:
    """Unit tests for rank_candidates."""

    def test_empty_items_returns_empty(self):
        """Empty items list → empty result."""
        assert rank_candidates("any query", []) == []

    def test_empty_query_is_safe(self):
        """Empty query string → no raise; all scores near zero; items returned."""
        items = [{"id": "x", "title": "X", "text": "some haystack text"}]
        result = rank_candidates("", items)
        assert len(result) == 1
        assert 0.0 <= result[0]["score"] <= 1.0

    def test_single_item_result_shape(self):
        """Single item → result has {id, title, score} keys; score in [0, 1]."""
        items = [{"id": "g-1", "title": "Goal One", "text": "improve legibility of the okr system"}]
        result = rank_candidates("legibility", items)
        assert len(result) == 1
        entry = result[0]
        assert entry["id"] == "g-1"
        assert entry["title"] == "Goal One"
        assert isinstance(entry["score"], float)
        assert 0.0 <= entry["score"] <= 1.0

    def test_score_formula_exact_values(self):
        """Score matches the reference formula byte-for-byte (0.7*ratio + 0.3*overlap, 4dp)."""
        query = "okr tracking legibility"
        haystack = "okr legibility system make okr tracking legible across the engineering team"
        items = [{"id": "g-leg", "title": "OKR Legibility", "text": haystack}]
        result = rank_candidates(query, items)
        assert result[0]["score"] == _expected_score(query, haystack)

    def test_score_formula_snapshot(self):
        """Score snapshot — byte-identical to goals_match.py's pre-refactor values.

        The snapshot values were captured before the extraction refactor and are the
        regression contract.  If the formula changes intentionally, update both this
        snapshot and the companion snapshot in test_goals_match.py::test_best_match_ranked_first.
        """
        # These haystacks replicate the goals_match.py test_best_match_ranked_first fixture:
        # unrelated goal: title + objective joined and lowercased
        h_unrelated = "unrelated goal completely different topic about infrastructure"
        # legibility goal: title + objective, no KRs
        h_legibility = "okr legibility system make okr tracking legible across the engineering team"
        query = "okr tracking legibility"

        items = [
            {"id": "unrelated", "title": "Unrelated Goal", "text": h_unrelated},
            {"id": "okr-legibility", "title": "OKR Legibility System", "text": h_legibility},
        ]
        result = rank_candidates(query, items)

        # Ranking: legibility must rank first.
        assert result[0]["id"] == "okr-legibility"
        assert result[1]["id"] == "unrelated"
        # Score snapshots (same values as test_goals_match.py regression gate).
        assert result[0]["score"] == 0.6     # okr-legibility
        assert result[1]["score"] == 0.1318  # unrelated

    def test_ranking_order_highest_first(self):
        """Higher-scoring item ranks first."""
        items = [
            {"id": "low", "title": "Low Match", "text": "completely unrelated xyz"},
            {"id": "high", "title": "High Match", "text": "reduce rendering latency below 100ms"},
        ]
        result = rank_candidates("reduce rendering latency", items)
        assert result[0]["id"] == "high"
        assert result[0]["score"] >= result[1]["score"]

    def test_tie_break_by_id_ascending(self):
        """Equal-score items are tie-broken by id ASCENDING."""
        # Craft two items with identical haystacks so scores are guaranteed equal.
        identical_text = "apple banana cherry"
        items = [
            {"id": "zzz-last", "title": "Z Item", "text": identical_text},
            {"id": "aaa-first", "title": "A Item", "text": identical_text},
        ]
        result = rank_candidates("apple", items)
        assert result[0]["id"] == "aaa-first"
        assert result[1]["id"] == "zzz-last"

    def test_score_range_always_zero_to_one(self):
        """Scores are always in [0.0, 1.0] regardless of input."""
        items = [
            {"id": "a", "title": "Alpha", "text": "alpha beta gamma"},
            {"id": "b", "title": "Beta", "text": ""},
            {"id": "c", "title": "Gamma", "text": "completely different delta epsilon"},
        ]
        result = rank_candidates("alpha beta", items)
        for entry in result:
            assert 0.0 <= entry["score"] <= 1.0

    def test_exact_match_score_is_one(self):
        """Query that exactly equals the haystack produces score 1.0."""
        query = "exact match phrase"
        items = [{"id": "exact", "title": "Exact", "text": query}]
        result = rank_candidates(query, items)
        assert result[0]["score"] == 1.0

    def test_items_not_mutated(self):
        """rank_candidates does not mutate the input items list."""
        original = [
            {"id": "g-1", "title": "Goal One", "text": "some objective text"},
            {"id": "g-2", "title": "Goal Two", "text": "another goal"},
        ]
        original_copy = [dict(item) for item in original]
        rank_candidates("objective", original)
        assert original == original_copy

    def test_kr_text_contributes_via_haystack(self):
        """Haystack that includes KR text (pre-built by caller) is ranked higher than control."""
        # Simulates how goals_match._collect_goals builds the haystack including KR texts.
        h_with_kr = "generic goal improve overall performance reduce perceptual rendering latency below 100ms"
        h_control = "unrelated control expand marketing reach into new demographics"
        items = [
            {"id": "g-kr", "title": "Generic Goal", "text": h_with_kr},
            {"id": "g-control", "title": "Unrelated Control", "text": h_control},
        ]
        result = rank_candidates("reduce perceptual rendering latency below 100ms", items)
        assert result[0]["id"] == "g-kr"
        # Score snapshot for this fixture.
        assert result[0]["score"] == _expected_score(
            "reduce perceptual rendering latency below 100ms", h_with_kr
        )


# ---------------------------------------------------------------------------
# Tests: resolve_candidate — score-load-bearing auto-resolution decision
# ---------------------------------------------------------------------------


def _many_plan_items(n: int = 50) -> list:
    """Build a many-item corpus (arity >> 1) with one distinctive title,
    the rest generic filler -- large enough that the OLD ``len(items) == 1``
    auto-resolve branch could never have fired, but a clear query should
    still auto-resolve against the score-load-bearing helper."""
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
    """Unit tests for resolve_candidate — the score-is-load-bearing decision
    helper that replaced the historical ``len(candidates)``-arity branch."""

    def test_many_items_clear_match_auto_resolves(self):
        """A 51-item corpus with one clearly-matching title still
        auto-resolves -- this is the exact regression the arithmetic
        auto-resolution defect (memo: DoE has 230 plans and 4 goals, so
        stamp mode nulled origin_plan_id on EVERY fork) is about; it must
        have failed against the old len(items) == 1 branch this helper
        replaces."""
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
        """A LONE candidate that fails the score floor must NOT be
        auto-resolved just for being alone -- this is the exact inversion of
        the historical ``len(items) == 1`` branch."""
        items = [{"id": "pln-only", "title": "Retire Legacy Auth Middleware", "text": "retire legacy auth middleware"}]
        result = resolve_candidate("2026-08-02_141130_some-slug", items)
        assert result["resolved_id"] is None
        assert result["reason"] == ResolutionReason.BELOW_THRESHOLD
        assert len(result["ranked"]) == 1

    def test_lone_candidate_above_floor_resolves(self):
        """A LONE candidate that DOES clear the floor resolves (the single-
        candidate case is not banned outright -- only auto-resolving on
        arity alone, ignoring score, is)."""
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
        """min_score / min_gap are keyword args callers can pin for
        deterministic tests independent of the module-level constants."""
        items = [{"id": "pln-only", "title": "Something", "text": "something"}]
        result = resolve_candidate("totally unrelated query", items, min_score=0.0, min_gap=0.0)
        assert result["resolved_id"] == "pln-only"
        assert result["reason"] is None

    # Review: coordinator:code-reviewer — both gates in resolve_candidate use
    # strict `<`, so score == min_score and gap == min_gap currently
    # auto-resolve (inclusive-pass at the edge). Nothing pinned that
    # convention before; the two tests below make it a named contract so a
    # future `<` -> `<=` edit is a visible diff, not a silent strictness flip.

    def test_score_exactly_at_floor_still_resolves(self):
        """A lone candidate whose score is EXACTLY min_score (not merely
        above it) auto-resolves -- resolve_candidate's floor check is a
        strict `<` (below-threshold), so an exact tie-to-the-floor is an
        inclusive pass, not a degrade. Pins the current boundary convention
        so a future `<` -> `<=` edit shows up as a failing test here rather
        than as a silent strictness change."""
        items = [{"id": "pln-only", "title": "Retire Legacy Auth Middleware", "text": "retire legacy auth middleware"}]
        ranked = rank_candidates("Retire Legacy Auth Middleware", items)
        exact_score = ranked[0]["score"]
        result = resolve_candidate(
            "Retire Legacy Auth Middleware", items, min_score=exact_score, min_gap=0.0
        )
        assert result["resolved_id"] == "pln-only"
        assert result["reason"] is None

    def test_gap_exactly_at_min_gap_still_resolves(self):
        """Two candidates whose score gap is EXACTLY min_gap (not merely
        wider than it) auto-resolve to the top candidate -- the too-close
        check is also a strict `<` (gap below min_gap), so an exact
        tie-to-the-gap-floor is an inclusive pass, not a degrade. Pins the
        current boundary convention alongside
        test_score_exactly_at_floor_still_resolves so both strictness edges
        of resolve_candidate are named contracts."""
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
