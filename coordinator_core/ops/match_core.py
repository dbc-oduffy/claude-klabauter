"""
coordinator_core.ops.match_core — Generic difflib-based candidate ranking core.

Purpose: Shared ranking kernel extracted from ``goals_match.py`` so that sibling
match ops (``plan.match_candidates``, ``handoff.match_candidates``, future kinds)
can reuse the same scoring formula without duplicating it.  This module is a
pure-compute helper — it does NOT register any op, does NOT read the filesystem
directly (enumerators are caller-supplied), and does NOT depend on IPC machinery.

Score formula (preserved byte-identical from goals_match.py):
    score = round(0.7 * ratio_score + 0.3 * overlap, 4)

where:
    ratio_score = difflib.SequenceMatcher(None, query.lower(), haystack).ratio()
    overlap     = len(query_tokens & haystack_tokens) / max(len(query_tokens), 1)

The overlap component compensates for SequenceMatcher's length-sensitivity when
haystacks differ in length (e.g. goals with many key results).

Enumerator protocol (per-kind): each kind-sibling op supplies a
``_collect_<kind>(worktree_root: Path) -> list[{id, title, text}]`` callable.
``id`` is the kind-specific identifier (goal_id, plan_id, …); callers remap the
generic ``id`` key to their kind-specific wire key after calling ``rank_candidates``.
``text`` is the pre-built haystack string — lowercase, space-joined from whichever
fields the kind considers significant.  ``title`` passes through to the ranked result.

Missing/empty enumerators MUST return ``[]`` (graceful-absent): callers never raise
on a missing artifact directory.  Mirror the pattern in ``initiatives_serve.py``
(``_collect_initiatives``) and ``handoff_children.py`` (worktree resolution +
early-return on absent directory).

COMPUTE_ONLY invariant: this module never writes files, issues git commands, or
mutates any coordinator substrate (DR-208).

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C1
"""

from __future__ import annotations

import difflib
from typing import List, Optional, TypedDict

# ---------------------------------------------------------------------------
# Auto-resolve thresholds
#
# Module-level so the bar for "confident enough to write without asking" is
# greppable and tunable in one place, rather than re-derived per call site.
#
# Calibrated empirically against this repo's real docs/plans/*.md corpus
# (191 well-formed plan items after quarantine) via a throwaway script that
# scored three query classes with rank_candidates and printed score/gap
# distributions (script deleted after use; not part of the shipped module):
#
#   (a) EXACT plan titles (15 queries, the must-auto-resolve case):
#       top score:  min=1.0000 max=1.0000 mean=1.0000  (exact match → ratio 1.0)
#       top gap:    min=0.2957 max=0.7161 mean=0.5769
#   (b) plausible partial/paraphrased titles (15 queries, a real fork's
#       match_text would plausibly carry):
#       top score:  min=0.6581 max=0.8045 mean=0.7236
#       top gap:    min=0.0129 max=0.5245 mean=0.2721
#       (the one low-gap outlier, 0.0129, is the generic query "Prior-Art
#       Check" — genuinely ambiguous against dozens of *.prior-art-check.md
#       plans; correctly landing on the too-close side, not a calibration
#       miss)
#   (c) timestamp-slug queries (3 queries, the must-NOT-resolve case — the
#       defect this calibration exists to prevent: stamp mode's old
#       match_text fallback was exactly this shape):
#       top score:  min=0.2000 max=0.2386 mean=0.2129
#       top gap:    min=0.0000 max=0.0198 mean=0.0080
#
# AUTO_RESOLVE_MIN_SCORE=0.5 sits with ~0.16 worst-case headroom below every
# (b) top score (min(b) - 0.5 = 0.6581 - 0.5 = 0.1581) and ~0.26 worst-case
# headroom above every (c) top score (0.5 - max(c) = 0.5 - 0.2386 = 0.2614) —
# an asymmetric margin, not a symmetric one: the floor sits noticeably closer
# to the (b) paraphrase class than to the (c) slug class, which is the
# direction that matters since a real fork's query belongs to (b). The score
# gate alone still separates prose queries from slug queries.
# Review: coordinator:code-reviewer — the prior "~0.26 on both sides" framing
# was arithmetically wrong on the (b) side and read as a symmetric safe zone
# when the margin is asymmetric; retune only against a re-run of the
# measurement above, not against this restated headroom alone.
# AUTO_RESOLVE_MIN_GAP
# =0.15 sits ~7x above every (c) gap (headroom against a near-tied slug
# false-positive) while still passing every (a) exact-title gap; it correctly
# rejects a handful of genuinely-ambiguous (b) queries (e.g. two
# similarly-named plans) into "too-close" rather than guessing.
# ---------------------------------------------------------------------------
AUTO_RESOLVE_MIN_SCORE = 0.5
AUTO_RESOLVE_MIN_GAP = 0.15


class ResolutionReason:
    """Machine-readable reasons for a failed auto-resolution — module-level
    string constants (not an enum) so callers can embed them directly in a
    ``degraded``/``needs_disambiguation`` reason string without an import of
    an enum type into wire-facing code."""

    NO_CANDIDATES = "no-candidates"
    BELOW_THRESHOLD = "below-threshold"
    TOO_CLOSE = "too-close"


class Resolution(TypedDict):
    """Return shape of ``resolve_candidate`` — the ranked list plus the
    auto-resolution decision over it."""

    ranked: List[dict]
    resolved_id: Optional[str]
    reason: Optional[str]


def rank_candidates(text: str, items: list) -> List[dict]:
    """Rank ``items`` by fuzzy similarity of ``text`` against each item's ``text`` field.

    Each item must be a ``dict`` with keys ``{"id", "title", "text"}``:
    - ``id``    — kind-specific identifier (callers remap to their wire key after ranking)
    - ``title`` — display title; passed through unchanged to the ranked result
    - ``text``  — pre-built haystack string (lowercase, space-joined); the field against
                  which ``text`` (the query) is scored

    Returns a list of ``{"id", "title", "score"}`` dicts sorted by score DESCENDING;
    ties are broken by ``id`` ASCENDING for determinism.  Score is a blended float:
    ``0.7 * SequenceMatcher ratio + 0.3 * token overlap``, rounded to 4 decimals,
    range 0.0–1.0.

    Safe on empty ``items`` (returns ``[]``).  Safe when ``text`` is empty (all scores
    will be near 0.0 — callers should short-circuit before calling if text is empty).

    Negative-spec:
    - Does NOT read the filesystem — haystacks are pre-built by the caller's enumerator.
    - Does NOT mutate ``items`` in-place — works on a copy.
    - Does NOT raise on empty input, zero-length query, or items with empty ``text``.
    """
    results: List[dict] = []
    query_lower = text.lower()
    query_tokens = set(query_lower.split())

    for item in items:
        haystack = item["text"]  # caller guarantees already lowercased
        ratio_score = difflib.SequenceMatcher(None, query_lower, haystack).ratio()
        haystack_tokens = set(haystack.split())
        overlap = len(query_tokens & haystack_tokens) / max(len(query_tokens), 1)
        score = round(0.7 * ratio_score + 0.3 * overlap, 4)
        results.append({"id": item["id"], "title": item["title"], "score": score})

    # Sort by score DESCENDING; tie-break by id ASCENDING for determinism.
    results.sort(key=lambda x: (-x["score"], x["id"]))
    return results


def resolve_candidate(
    text: str,
    items: list,
    *,
    min_score: float = AUTO_RESOLVE_MIN_SCORE,
    min_gap: float = AUTO_RESOLVE_MIN_GAP,
) -> Resolution:
    """Rank ``items`` against ``text`` (via ``rank_candidates``, reused rather
    than duplicated) and decide whether the top candidate is confident enough
    to auto-resolve without asking.

    This is the score-is-load-bearing counterpart to the historical
    ``len(candidates)``-arity branch it replaces: the raw COUNT of items in a
    directory says nothing about whether any of them actually matches
    ``text`` — a repo with 230 plans and a repo with 1 plan should both be
    able to auto-resolve when the query clearly names one of them, and both
    should refuse to when it doesn't.

    Returns a ``Resolution`` dict:
        ``ranked``      — the full DESC-sorted ``rank_candidates`` output
                          (always populated, even when unresolved — callers
                          use it to build a ``candidates``/``degraded`` payload).
        ``resolved_id``  — the winning candidate's ``id`` when auto-resolved,
                          else ``None``.
        ``reason``       — ``None`` when resolved; otherwise one of
                          ``ResolutionReason.{NO_CANDIDATES,BELOW_THRESHOLD,TOO_CLOSE}``.

    Decision rule:
    - Empty ``items`` → unresolved, reason ``NO_CANDIDATES`` (nothing to rank —
      this is the graceful-absent case, distinct from the two below: the query
      may have been perfectly informative, there was simply nothing to score
      it against).
    - Top candidate's score ``< min_score`` → unresolved, reason
      ``BELOW_THRESHOLD`` (nothing scored well enough — a single candidate
      that fails the floor is NOT auto-resolved just for being alone).
    - Top candidate clears the floor but (with 2+ candidates) leads the
      runner-up by less than ``min_gap`` → unresolved, reason ``TOO_CLOSE``
      (a genuine tie between two plausible matches).
    - Otherwise → resolved to the top candidate's ``id``, reason ``None``.

    Negative-spec:
    - Does NOT read the filesystem — ranking is delegated to
      ``rank_candidates``, which is itself filesystem-free (see its own
      negative-spec); ``items`` is caller-supplied.
    - Does NOT raise on empty ``items`` or empty ``text``.
    - Does NOT treat "exactly one candidate" as sufficient on its own — a lone
      candidate still has to clear ``min_score``.
    """
    ranked = rank_candidates(text, items)
    if not ranked:
        return {"ranked": ranked, "resolved_id": None, "reason": ResolutionReason.NO_CANDIDATES}
    top = ranked[0]
    if top["score"] < min_score:
        return {"ranked": ranked, "resolved_id": None, "reason": ResolutionReason.BELOW_THRESHOLD}
    if len(ranked) > 1:
        runner_up = ranked[1]
        if top["score"] - runner_up["score"] < min_gap:
            return {"ranked": ranked, "resolved_id": None, "reason": ResolutionReason.TOO_CLOSE}
    return {"ranked": ranked, "resolved_id": top["id"], "reason": None}
