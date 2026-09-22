"""
coordinator_core.ops.dispatch_emit.tests.test_disposition_vocabulary

Purpose: pins issue coordinator-klabauter#45 class A -- the disposition
vocabulary drift between `skills/mise-en-place` Phase 1 and
`inventory_mint.py`'s classifier, and its two failure directions:

  1. `pending` (skill-documented LIVE word) must reach the minted spine as
     LIVE, not silently vanish as CLOSED.
  2. `routed-out` (skill-documented, hyphenated) and `routed out`
     (unhyphenated, what a scout naturally writes) must both BLOCK a
     dependent row -- neither spelling may fall through to
     closed-satisfied, which would silently unblock a row whose premise
     was withheld.
  3. A disposition matching none of the recognized vocabularies must raise
     `UnrecognizedDispositionError`, naming the row and the raw text,
     rather than silently classifying it CLOSED-SATISFIED.

Drives every case through `mint_rows` (the production call site
`mint_spine` -> `mint_rows` -> `_resolve_dep_kinds` -> `_raw_disposition_kind`
actually uses), not the private classifier alone -- a fix that only passed
its own unit tests while `mint_rows` still silently dropped/unblocked a row
would leave the reported bug live.

Negative-spec: does not touch disk, does not call `mint_spine` (which reads
a file) -- exercises `parse_chunk_table`'s row-dict shape directly against
`mint_rows`.
"""

from __future__ import annotations

import warnings

import pytest

from coordinator_core.ops.dispatch_emit.inventory_mint import (
    UnrecognizedDispositionError,
    mint_rows,
)


def _row(row_id: str, disposition: str, deps: str = "—", footprint: str = None) -> dict:
    # `deps` cells hold plain, un-backtick-quoted ids (matching
    # test_inventory_mint.py's fixture shape and `_split_id_list`, which
    # does not strip backticks).
    return {
        "id": f"`{row_id}`",
        "spec path": "`docs/plans/x.md` (S1)",
        "summary": f"Do {row_id}",
        "footprint": footprint if footprint is not None else f"`src/{row_id}.py`",
        "deps": deps,
        "verification": "it works",
        "complexity": "low",
        "disposition": disposition,
    }


def test_pending_is_live_reaches_minted_spine():
    rows = [_row("C1", "pending — not yet dispatched")]
    minted = mint_rows(rows)
    assert [r["id"] for r in minted] == ["C1"]


def test_queued_and_in_progress_still_live():
    rows = [
        _row("C1", "queued for next wave"),
        _row("C2", "in_progress, executor dispatched"),
    ]
    minted = mint_rows(rows)
    assert {r["id"] for r in minted} == {"C1", "C2"}


def test_routed_out_hyphenated_blocks_dependent_row():
    rows = [
        _row("C1", "routed-out: premise withheld at certification"),
        _row("C2", "pending", deps="C1"),
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        minted = mint_rows(rows)
    assert [r["id"] for r in minted] == []
    assert any("C2" in str(w.message) and "C1" in str(w.message) for w in caught)


def test_routed_out_unhyphenated_also_blocks_dependent_row():
    rows = [
        _row("C1", "routed out: premise withheld at certification"),
        _row("C2", "pending", deps="C1"),
    ]
    minted = mint_rows(rows)
    assert [r["id"] for r in minted] == []


def test_known_closed_satisfied_drops_edge_without_blocking():
    rows = [
        _row("C1", "already-fixed upstream"),
        _row("C2", "pending", deps="C1"),
    ]
    minted = mint_rows(rows)
    ids = [r["id"] for r in minted]
    assert ids == ["C2"]
    assert "depends_on" not in minted[0]


def test_unrecognized_disposition_raises_naming_row_and_text():
    rows = [_row("C7", "In Progress (typo'd capitalization variant)")]
    # "in progress" (space, capitalized) does not start with "in_progress"
    with pytest.raises(UnrecognizedDispositionError) as exc_info:
        mint_rows(rows)
    message = str(exc_info.value)
    assert "C7" in message
    assert "In Progress (typo'd capitalization variant)" in message


def test_unrecognized_disposition_on_a_dependency_also_raises():
    rows = [
        _row("C1", "wat-is-this-word"),
        _row("C2", "pending", deps="C1"),
    ]
    with pytest.raises(UnrecognizedDispositionError) as exc_info:
        mint_rows(rows)
    assert "C1" in str(exc_info.value)
