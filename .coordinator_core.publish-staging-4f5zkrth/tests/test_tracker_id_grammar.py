"""
Unit tests for coordinator_core.tracker_id_grammar.ITEM_ID_PATTERN / is_item_id.

Review: code-reviewer sat-07-review.slice-A Finding 2 — the module is a new
leaf load-bearing in two places (`tracker_entities.mint_item_id`'s charset
guard and `ops.emit.closure_trailer`'s trailer pattern table) but was only
exercised indirectly, via `test_closure_trailer.py`'s happy-path round-trip.
This file adds direct boundary/negative coverage against the grammar the
module's own docstring declares: ``itm-<YYYYMMDD>-<slug 1..32 chars,
[a-z0-9-]>-<nonce, 6 lowercase hex>-<digest, 12 lowercase hex>``, anchored
both ends (never used with ``.search``).
"""

from __future__ import annotations

from coordinator_core.tracker_entities import mint_item_id
from coordinator_core.tracker_id_grammar import is_item_id


def test_real_minted_id_matches() -> None:
    minted = mint_item_id(
        "Fix the thing", "some body text", "2026-08-18T12:00:00+00:00"
    )
    assert is_item_id(minted)


def test_jira_shaped_id_does_not_match() -> None:
    assert not is_item_id("RECS-1")
    assert not is_item_id("ABC-123")


def test_roadmap_stub_id_does_not_match() -> None:
    assert not is_item_id("sat-07")


def test_slug_at_minimum_length_matches() -> None:
    assert is_item_id("itm-20260818-a-a1b2c3-0123456789ab")


def test_slug_at_maximum_length_matches() -> None:
    slug = "a" * 32
    assert is_item_id(f"itm-20260818-{slug}-a1b2c3-0123456789ab")


def test_slug_over_maximum_length_does_not_match() -> None:
    slug = "a" * 33
    assert not is_item_id(f"itm-20260818-{slug}-a1b2c3-0123456789ab")


def test_empty_slug_does_not_match() -> None:
    assert not is_item_id("itm-20260818--a1b2c3-0123456789ab")


def test_nonce_too_short_does_not_match() -> None:
    assert not is_item_id("itm-20260818-fix-the-thing-a1b2c-0123456789ab")


def test_nonce_too_long_does_not_match() -> None:
    assert not is_item_id("itm-20260818-fix-the-thing-a1b2c33-0123456789ab")


def test_digest_too_short_does_not_match() -> None:
    assert not is_item_id("itm-20260818-fix-the-thing-a1b2c3-0123456789a")


def test_digest_too_long_does_not_match() -> None:
    assert not is_item_id("itm-20260818-fix-the-thing-a1b2c3-0123456789abc")


def test_uppercase_in_slug_does_not_match() -> None:
    assert not is_item_id("itm-20260818-Fix-The-Thing-a1b2c3-0123456789ab")


def test_uppercase_in_nonce_does_not_match() -> None:
    assert not is_item_id("itm-20260818-fix-the-thing-A1B2C3-0123456789ab")


def test_uppercase_in_digest_does_not_match() -> None:
    assert not is_item_id("itm-20260818-fix-the-thing-a1b2c3-0123456789AB")


def test_malformed_date_segment_does_not_match() -> None:
    # 7 digits instead of 8 (YYYYMMD)
    assert not is_item_id("itm-2026081-fix-the-thing-a1b2c3-0123456789ab")
    # non-digit character in the date
    assert not is_item_id("itm-2026081x-fix-the-thing-a1b2c3-0123456789ab")


def test_anchored_id_embedded_in_surrounding_text_does_not_match() -> None:
    minted = mint_item_id(
        "Fix the thing", "some body text", "2026-08-18T12:00:00+00:00"
    )
    assert not is_item_id(f"see {minted} for details")
    assert not is_item_id(f"{minted} trailing text")
    assert not is_item_id(f" {minted}")
