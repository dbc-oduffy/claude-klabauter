"""
Tests for `mint_rows` keeping every `depends_on` edge on write-overlap
(plan row IBMFR-R24, `docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md`).

`mint_rows` used to drop a `deps`-named edge whenever the two rows' `writes`
sets intersected, on the rationale that `pathspec`/`wave_map.py`'s own
collision-based sequencing already implies the ordering "structurally". That
branch is gone: every edge `deps` names survives (minus a `closed-satisfied`
target, unrelated to this fix), and a write-overlap pair with NO explicit
`deps` edge between them now gets one added, from the later row (table
order) to the earlier one it shares a write with -- see
`inventory_mint.py :: mint_rows`'s own docstring update.
"""

from __future__ import annotations

import textwrap

from coordinator_core.ops.dispatch_emit import inventory_mint as im

# ---------------------------------------------------------------------------
# explicit deps edge across an overlapping write -- kept, not dropped
# ---------------------------------------------------------------------------

_EXPLICIT_OVERLAP_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260926T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | shared file chunk | `coordinator_core/fixture_shared.py` | — | scoped pytest | S | in_progress |
    | C2 | `docs/plans/fixture.md` | overlapping chunk | `coordinator_core/fixture_shared.py`, `coordinator_core/fixture_c2.py` | C1 | scoped pytest | S | in_progress |
    | C3 | `docs/plans/fixture.md` | non-overlapping chunk | `coordinator_core/fixture_c3.py` | C1 | scoped pytest | S | in_progress |
    """
)


def test_explicit_deps_edge_across_write_overlap_is_kept_not_dropped():
    rows = im.parse_chunk_table(_EXPLICIT_OVERLAP_INVENTORY)
    minted = im.mint_rows(rows)
    by_id = {row["id"]: row for row in minted}

    # C2 shares fixture_shared.py with C1 AND names C1 in deps -- the edge
    # is kept, not dropped for overlap.
    assert by_id["C2"]["depends_on"] == [
        {"chunk": "C1", "gate_kind": "output-consumption-runtime"}
    ]

    # C3 shares nothing with C1 -- its ordinary deps edge is unaffected.
    assert by_id["C3"]["depends_on"] == [
        {"chunk": "C1", "gate_kind": "output-consumption-runtime"}
    ]


# ---------------------------------------------------------------------------
# write-overlap with NO explicit deps edge -- one is added on top
# ---------------------------------------------------------------------------

# The sender's fifa02 shape: C5 writes a shared file first; C13, later in
# the table, writes the same file but names no `deps` edge to C5 at all.
_IMPLICIT_OVERLAP_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260926T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C5 | `docs/plans/fifa02.md` | first writer of the shared file | `engine/src/fixture_shared.ts` | — | scoped pytest | S | in_progress |
    | C13 | `docs/plans/fifa02.md` | second writer of the shared file, no explicit dep | `engine/src/fixture_shared.ts` | — | scoped pytest | S | in_progress |
    """
)


def test_write_overlap_with_no_explicit_dep_gets_an_edge_added():
    rows = im.parse_chunk_table(_IMPLICIT_OVERLAP_INVENTORY)
    minted = im.mint_rows(rows)
    by_id = {row["id"]: row for row in minted}

    # C5 is the first writer -- it gets no edge to a row that comes after it.
    assert "depends_on" not in by_id["C5"]

    # C13 shares fixture_shared.ts with the earlier C5 and named no explicit
    # dep -- mint_rows adds the edge on top, scheduling C13 (dependent)
    # after C5 (dependency).
    assert by_id["C13"]["depends_on"] == [
        {"chunk": "C5", "gate_kind": "output-consumption-runtime"}
    ]
