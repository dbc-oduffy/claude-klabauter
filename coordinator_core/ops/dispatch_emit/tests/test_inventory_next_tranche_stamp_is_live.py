"""
Purpose: a `routed-out: next tranche ...` stamp classifies LIVE so
`depends_on_plan` orders it; any other `routed-out` reason stays blocking.
"""

from __future__ import annotations

import warnings

import pytest

from coordinator_core.ops.dispatch_emit.inventory_mint import (
    _DEP_KIND_LIVE,
    _DEP_KIND_ROUTED_OUT,
    _is_live_disposition,
    _raw_disposition_kind,
    mint_rows,
)


@pytest.mark.parametrize(
    "raw",
    [
        "routed-out: next tranche, waits on uncoded I10.C4",
        "routed out: next tranche",
        "Routed-Out: Next Tranche - waits",
        "  ROUTED OUT: NEXT TRANCHE",
    ],
)
def test_next_tranche_stamp_is_live(raw):
    assert _is_live_disposition(raw)
    assert _raw_disposition_kind("I12", raw) == _DEP_KIND_LIVE


@pytest.mark.parametrize(
    "raw",
    [
        "routed-out: writes outside this repo",
        "routed out: premise withheld",
        "routed-out",
        "routed-out: not next tranche",
    ],
)
def test_other_routed_out_still_blocks(raw):
    assert not _is_live_disposition(raw)
    assert _raw_disposition_kind("I12", raw) == _DEP_KIND_ROUTED_OUT


def _row(row_id, disposition, deps="—"):
    return {
        "id": f"`{row_id}`",
        "spec path": "`docs/plans/x.md` (S1)",
        "summary": f"Do {row_id}",
        "footprint": f"`src/{row_id}.py`",
        "deps": deps,
        "verification": "it works",
        "complexity": "low",
        "disposition": disposition,
    }


def test_next_tranche_row_mints_and_cross_repo_dependent_routes_out():
    rows = [
        _row("A", "routed-out: next tranche, waits on A0"),
        _row("B", "routed-out: writes outside this repo"),
        _row("C", "pending", deps="B"),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        minted = mint_rows(rows)
    ids = [r["id"] for r in minted]
    assert "A" in ids
    assert "B" not in ids and "C" not in ids
