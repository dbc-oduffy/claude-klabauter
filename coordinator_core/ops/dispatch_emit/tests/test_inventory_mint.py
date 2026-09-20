"""
Tests for coordinator_core.ops.dispatch_emit.inventory_mint.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § S1-C4.
"""

from __future__ import annotations

import textwrap

import pytest

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.emit import _BRIEF_PRECEDENCE_CLAUSE, emit_script
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit


def _write_inventory(tmp_path, body: str, name: str = "20260918T000000-fixture.md"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# live/closed disposition classification
# ---------------------------------------------------------------------------

_LIVE_AND_CLOSED_INVENTORY = textwrap.dedent(
    """\
    ---
    kind: mise-inventory
    run_id: 20260918T000000-fixture
    ---

    # Mise-en-place inventory — fixture

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | first fixture chunk | `coordinator_core/fixture_a.py` | — | scoped pytest | S | in_progress |
    | C2 | `docs/plans/fixture.md` | second fixture chunk | `coordinator_core/fixture_b.py` | C1 | scoped pytest | S | queued — wave B |
    | C3 | `docs/plans/fixture.md` | terminal EM row | `coordinator_core/fixture_c.py` | C1, C2 | EM-run | S | pending — not yet started |
    | C4 | `docs/plans/fixture.md` | routed-out chunk | `coordinator_core/fixture_d.py` | — | scoped pytest | S | routed out — premise moved |
    """
)


def test_live_dispositions_including_pending_are_minted_routed_out_is_dropped():
    rows = im.parse_chunk_table(_LIVE_AND_CLOSED_INVENTORY)
    minted = im.mint_rows(rows)
    minted_ids = {row["id"] for row in minted}

    assert minted_ids == {"C1", "C2", "C3"}


def test_pending_is_live_routed_out_is_classified_closed():
    assert im._is_live_disposition("pending — not yet started") is True
    assert im._is_live_disposition("routed out — premise moved") is False
    assert im._is_live_disposition("in_progress") is True
    assert im._is_live_disposition("queued — wave B") is True


# ---------------------------------------------------------------------------
# unreadable footprint refuses
# ---------------------------------------------------------------------------

_UNREADABLE_FOOTPRINT_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | broken footprint | coordinator_core/fixture_a.py | — | scoped pytest | S | in_progress |
    """
)


def test_unreadable_footprint_refuses():
    rows = im.parse_chunk_table(_UNREADABLE_FOOTPRINT_INVENTORY)
    with pytest.raises(im.FootprintUnreadableError):
        im.mint_rows(rows)


def test_empty_footprint_on_a_live_row_refuses():
    rows = im.parse_chunk_table(_LIVE_AND_CLOSED_INVENTORY)
    # Flip C3 to LIVE with an empty footprint to exercise the empty-writes
    # refusal -- C3 carries a real footprint in the shared fixture now that
    # `pending` is itself LIVE, so this test clears it back to empty.
    for row in rows:
        if row["id"] == "C3":
            row["disposition"] = "in_progress"
            row["footprint"] = "—"
    with pytest.raises(im.FootprintUnreadableError):
        im.mint_rows(rows)


def test_chunk_table_absent_raises():
    with pytest.raises(im.ChunkTableAbsentError):
        im.parse_chunk_table("---\nrun_id: x\n---\n\nNo table here.\n")


# ---------------------------------------------------------------------------
# coordinator-klabauter#25 class 1: an escaped pipe inside a cell must not
# split the column.
# ---------------------------------------------------------------------------

_ESCAPED_PIPE_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | a \\| b summary | `coordinator_core/fixture_a.py` | — | scoped pytest | S | in_progress |
    """
)


def test_escaped_pipe_inside_a_cell_does_not_split_the_column():
    rows = im.parse_chunk_table(_ESCAPED_PIPE_INVENTORY)
    assert len(rows) == 1
    assert rows[0]["summary"] == "a | b summary"


# ---------------------------------------------------------------------------
# coordinator-klabauter#25 class 2: a backticked path followed by trailing
# prose is accepted, the path kept and the prose discarded; a cell with no
# backticked path at all still refuses.
# ---------------------------------------------------------------------------

_ANNOTATED_FOOTPRINT_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | annotated footprint | `coordinator_core/fixture_a.py` (verify only, no edit) | — | scoped pytest | S | in_progress |
    """
)


def test_backticked_path_with_trailing_prose_is_kept_prose_discarded():
    rows = im.parse_chunk_table(_ANNOTATED_FOOTPRINT_INVENTORY)
    minted = im.mint_rows(rows)
    assert minted[0]["writes"] == ["coordinator_core/fixture_a.py"]


def test_footprint_with_no_backticked_path_names_the_rule():
    rows = im.parse_chunk_table(_UNREADABLE_FOOTPRINT_INVENTORY)
    with pytest.raises(im.FootprintUnreadableError, match="backtick-quoted path"):
        im.mint_rows(rows)


# ---------------------------------------------------------------------------
# coordinator-klabauter#25 class 3: a glob footprint refuses AT MINT,
# naming the rule.
# ---------------------------------------------------------------------------

_GLOB_FOOTPRINT_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | glob footprint | `tests/**/*.py` | — | scoped pytest | S | in_progress |
    """
)


def test_glob_footprint_refuses_at_mint_naming_the_rule():
    rows = im.parse_chunk_table(_GLOB_FOOTPRINT_INVENTORY)
    with pytest.raises(im.GlobFootprintError, match="unbounded pathspec"):
        im.mint_rows(rows)


# ---------------------------------------------------------------------------
# coordinator-klabauter#25 class 4: a directory-shaped entry refuses at
# mint, naming the rule rather than just the entry.
# ---------------------------------------------------------------------------

_DIRECTORY_FOOTPRINT_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | directory footprint | `docs/wiki/` | — | scoped pytest | S | in_progress |
    """
)


def test_directory_shaped_footprint_refuses_at_mint_naming_the_rule():
    rows = im.parse_chunk_table(_DIRECTORY_FOOTPRINT_INVENTORY)
    with pytest.raises(
        im.DirectoryShapedFootprintError, match="directory pathspec"
    ):
        im.mint_rows(rows)


# ---------------------------------------------------------------------------
# coordinator-klabauter#45's smaller Class B note: the no-trailing-separator
# spelling of a directory-shaped entry (`cross-repo/outbox`, no `/`) used to
# pass this same rung and only die later, at the workflow's claimability
# preflight -- both spellings now refuse here, at mint time.
# ---------------------------------------------------------------------------

_NO_TRAILING_SEP_EXISTING_DIRECTORY_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | existing dir, no trailing sep | `docs/wiki` | — | scoped pytest | S | in_progress |
    """
)


def test_directory_shaped_footprint_without_trailing_separator_refuses_at_mint():
    rows = im.parse_chunk_table(_NO_TRAILING_SEP_EXISTING_DIRECTORY_INVENTORY)
    with pytest.raises(
        im.DirectoryShapedFootprintError, match="directory pathspec"
    ):
        im.mint_rows(rows)


_NOT_YET_EXISTING_PATH_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | not-yet-existing file | `coordinator_core/ops/dispatch_emit/tests/fixture_does_not_exist_yet.py` | — | scoped pytest | S | in_progress |
    """
)


def test_not_yet_existing_path_is_not_refused_as_directory_shaped():
    rows = im.parse_chunk_table(_NOT_YET_EXISTING_PATH_INVENTORY)
    minted = im.mint_rows(rows)
    assert minted[0]["surface"] == (
        "coordinator_core/ops/dispatch_emit/tests/fixture_does_not_exist_yet.py"
    )


# ---------------------------------------------------------------------------
# coordinator-klabauter#25 class 5: a dep naming a closed row.
# ---------------------------------------------------------------------------

_DEP_ON_LANDED_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | already landed | `coordinator_core/fixture_a.py` | — | scoped pytest | S | landed — shipped in an earlier wave |
    | C2 | `docs/plans/fixture.md` | depends on landed row | `coordinator_core/fixture_b.py` | C1 | scoped pytest | S | in_progress |
    """
)


def test_dep_on_a_landed_row_is_satisfied_edge_dropped():
    rows = im.parse_chunk_table(_DEP_ON_LANDED_INVENTORY)
    minted = im.mint_rows(rows)
    assert {row["id"] for row in minted} == {"C2"}
    assert "depends_on" not in minted[0]


_DEP_ON_ROUTED_OUT_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | routed out row | `coordinator_core/fixture_a.py` | — | scoped pytest | S | routed out — premise moved |
    | C2 | `docs/plans/fixture.md` | blocked by routed-out dep | `coordinator_core/fixture_b.py` | C1 | scoped pytest | S | in_progress |
    | C3 | `docs/plans/fixture.md` | transitively blocked | `coordinator_core/fixture_c.py` | C2 | scoped pytest | S | in_progress |
    """
)


def test_dep_on_a_routed_out_row_routes_the_dependent_out_transitively():
    rows = im.parse_chunk_table(_DEP_ON_ROUTED_OUT_INVENTORY)
    with pytest.warns(UserWarning, match="routed out"):
        minted = im.mint_rows(rows)
    assert minted == []


# ---------------------------------------------------------------------------
# a Deps edge explained by write overlap is dropped
# ---------------------------------------------------------------------------

_WRITE_OVERLAP_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | shared file chunk | `coordinator_core/fixture_shared.py` | — | scoped pytest | S | in_progress |
    | C2 | `docs/plans/fixture.md` | overlapping chunk | `coordinator_core/fixture_shared.py`, `coordinator_core/fixture_c2.py` | C1 | scoped pytest | S | in_progress |
    | C3 | `docs/plans/fixture.md` | non-overlapping chunk | `coordinator_core/fixture_c3.py` | C1 | scoped pytest | S | in_progress |
    """
)


def test_write_overlap_edge_is_dropped_non_overlapping_edge_survives():
    rows = im.parse_chunk_table(_WRITE_OVERLAP_INVENTORY)
    minted = im.mint_rows(rows)
    by_id = {row["id"]: row for row in minted}

    # C2 shares fixture_shared.py with C1 -- the C1 edge is dropped.
    assert "depends_on" not in by_id["C2"]

    # C3 shares nothing with C1 -- the edge survives.
    assert by_id["C3"]["depends_on"] == [
        {"chunk": "C1", "gate_kind": "output-consumption-runtime"}
    ]


# ---------------------------------------------------------------------------
# change_kind inference
# ---------------------------------------------------------------------------

_MIXED_CHANGE_KIND_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20260918T000000-fixture
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | doc-only chunk | `docs/research/fixture.md` | — | doc present | S | in_progress |
    | C2 | `docs/plans/fixture.md` | code chunk | `coordinator_core/fixture_a.py` | — | scoped pytest | S | in_progress |
    """
)


def test_change_kind_inferred_from_footprint_extension():
    rows = im.parse_chunk_table(_MIXED_CHANGE_KIND_INVENTORY)
    minted = im.mint_rows(rows)
    by_id = {row["id"]: row for row in minted}

    assert by_id["C1"]["change_kind"] == "doc-edit"
    assert by_id["C2"]["change_kind"] == "code-edit"


# ---------------------------------------------------------------------------
# mint_spine: end-to-end text + path, deliverable_id inheritance
# ---------------------------------------------------------------------------


def test_mint_spine_writes_no_disk_returns_text_and_run_id_path(tmp_path):
    inventory_path = _write_inventory(tmp_path, _LIVE_AND_CLOSED_INVENTORY)

    spine_text, spine_path = im.mint_spine(str(inventory_path))

    assert spine_path == tmp_path / "20260918T000000-fixture.spine.md"
    assert not spine_path.exists()
    assert "## Tasks" in spine_text
    assert "```yaml plan-tasks" in spine_text
    assert "id: C1" in spine_text
    assert "id: C2" in spine_text
    assert "id: C3" in spine_text  # live (pending is LIVE vocabulary)
    assert "id: C4" not in spine_text  # closed (routed out)


def test_mint_spine_inherits_deliverable_id_from_source_baton_never_mints(tmp_path):
    baton_path = tmp_path / "baton.md"
    baton_path.write_text(
        "---\ndeliverable_id: dlv-fixture-baton-abc123\n---\n\nBaton.\n",
        encoding="utf-8",
    )
    inventory_text = _LIVE_AND_CLOSED_INVENTORY.replace(
        "run_id: 20260918T000000-fixture\n",
        "run_id: 20260918T000000-fixture\nsource_baton: baton.md\n",
    )
    inventory_path = _write_inventory(tmp_path, inventory_text)

    spine_text, _ = im.mint_spine(str(inventory_path))

    assert "deliverable_id: dlv-fixture-baton-abc123" in spine_text


def test_mint_spine_omits_deliverable_id_when_no_source_baton(tmp_path):
    inventory_path = _write_inventory(tmp_path, _LIVE_AND_CLOSED_INVENTORY)

    spine_text, _ = im.mint_spine(str(inventory_path))

    assert "deliverable_id" not in spine_text


# ---------------------------------------------------------------------------
# spine parses back through the plan-tasks fenced-block reader, and every
# agent prompt of an inventory-minted emission still leads with
# _BRIEF_PRECEDENCE_CLAUSE
# ---------------------------------------------------------------------------


def test_minted_spine_round_trips_through_read_spine(tmp_path):
    from coordinator_core.ops.dispatch_emit.spine_read import read_spine

    inventory_path = _write_inventory(tmp_path, _WRITE_OVERLAP_INVENTORY)
    spine_text, spine_path = im.mint_spine(str(inventory_path))
    spine_path.write_text(spine_text, encoding="utf-8")

    emitter_rows = read_spine(str(spine_path))
    assert {row.id for row in emitter_rows} == {"C1", "C2", "C3"}


def test_dispatch_emit_inventory_path_emits_prompts_leading_with_precedence_clause(tmp_path):
    inventory_path = _write_inventory(tmp_path, _WRITE_OVERLAP_INVENTORY)
    output_path = tmp_path / "fixture.workflow.mjs"

    result = _dispatch_emit(
        {
            "inventory_path": str(inventory_path),
            "output_path": str(output_path),
            "target_root": str(tmp_path),
        }
    )

    assert result["ok"] is True
    script = output_path.read_text(encoding="utf-8")

    # Every dispatched agent prompt embedded in the emitted script leads
    # with the brief-precedence clause -- same contract a hand-authored
    # plan_path emission carries.
    assert _BRIEF_PRECEDENCE_CLAUSE in script
    for chunk_id in ("C1", "C2", "C3"):
        assert chunk_id in script


def test_dispatch_emit_rejects_both_plan_path_and_inventory_path(tmp_path):
    from coordinator_core.ops.dispatch_emit.op import InventoryPathConflictError

    inventory_path = _write_inventory(tmp_path, _WRITE_OVERLAP_INVENTORY)
    with pytest.raises(InventoryPathConflictError):
        _dispatch_emit(
            {
                "plan_path": "docs/plans/whatever.md",
                "inventory_path": str(inventory_path),
                "output_path": str(tmp_path / "out.mjs"),
            }
        )
