"""
Unit tests for coordinator_core.ops.emit.closure_trailer.parse_closure_trailers.

Spec: docs/plans/2026-07-17-commit-closure-emission-fact.md, task C1, AC1.
Covers: value present, absent (empty list -> []), multiple values, and a
lowercased variant — the chunk's self-authored test per the plan body.
"""

from __future__ import annotations

from coordinator_core.ops.emit.closure_trailer import parse_closure_trailers
from coordinator_core.tracker_entities import mint_item_id


def test_minted_item_id_round_trips_through_trailer_parser() -> None:
    # C1 AC1 — a real `mint_item_id` output, not a hand-authored string that
    # merely happens to match the new pattern row. Proves the mint path and
    # the trailer grammar recognize the same grammar rather than two
    # independently-hand-copied ones that can drift apart.
    minted = mint_item_id(
        "Fix the thing", "some body text", "2026-08-18T12:00:00+00:00"
    )
    assert parse_closure_trailers([minted]) == [minted]


def test_single_value_present() -> None:
    assert parse_closure_trailers(["RECS-42"]) == ["RECS-42"]


def test_absent_values_yield_empty_list() -> None:
    assert parse_closure_trailers([]) == []


def test_no_recognized_pattern_yields_empty_list() -> None:
    assert parse_closure_trailers(["this closes the loop"]) == []


def test_multiple_values_preserve_input_order() -> None:
    assert parse_closure_trailers(["RECS-42", "OPS-7", "ABC-100"]) == [
        "RECS-42",
        "OPS-7",
        "ABC-100",
    ]


def test_lowercased_variant_recognized() -> None:
    assert parse_closure_trailers(["recs-42"]) == ["recs-42"]


def test_mixed_present_and_absent_preserves_order_and_skips_non_matches() -> None:
    # Review: code-reviewer (Finding 4) — the existing "multiple values" test only covered an
    # all-matching list; this proves "skip non-matching, keep matching, preserve match order"
    # rather than a hypothetical all-or-nothing bug where one non-match short-circuits the list.
    assert parse_closure_trailers(["RECS-42", "this closes the loop", "OPS-7"]) == [
        "RECS-42",
        "OPS-7",
    ]
