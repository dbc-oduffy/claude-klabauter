"""
Tests for the optional `spec path` / `complexity` Chunk-table columns.

Spec backlink: docs/plans/2026-10-06-dispatch-emit-refuses-at-emit-time.md (I206.C2).
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.emit import _row_source_plan
from coordinator_core.ops.dispatch_emit.op import _has_live_row, _live_spec_paths


def _inv(header: str, *rows: str) -> str:
    sep = "|" + "|".join("---" for _ in header.strip("|").split("|")) + "|"
    return "## Chunk table\n\n" + "\n".join([header, sep, *rows]) + "\n"


_NO_OPTIONAL = _inv(
    "| id | summary | footprint | deps | verification | disposition |",
    "| C1 | do a thing | `a/b.py` | - | pytest | pending |",
)


def test_table_without_optional_columns_mints_with_no_spec_or_complexity_line():
    rows = im.parse_chunk_table(_NO_OPTIONAL)
    assert rows[0]["spec path"] == "" and rows[0]["complexity"] == ""
    (minted,) = im.mint_rows(rows)
    assert "Spec:" not in minted["body"]
    assert "Complexity:" not in minted["body"]
    assert _row_source_plan(SimpleNamespace(body=minted["body"])) is None


def test_cx_header_is_read_as_complexity():
    text = _inv(
        "| id | spec path | summary | footprint | deps | verification | cx | disposition |",
        "| C1 | `docs/plans/p.md` | s | `a.py` | - | v | M | pending |",
    )
    (row,) = im.parse_chunk_table(text)
    assert row["complexity"] == "M"
    (minted,) = im.mint_rows([row])
    assert "Complexity: M\n" in minted["body"]


def test_dash_cells_read_as_absent():
    for cell in ("—", "-", ""):
        text = _inv(
            "| id | spec path | summary | footprint | deps | verification | complexity | disposition |",
            f"| C1 | {cell} | s | `a.py` | - | v | {cell} | pending |",
        )
        (row,) = im.parse_chunk_table(text)
        assert row["spec path"] == "" and row["complexity"] == ""


def test_spec_less_row_is_not_expanded(tmp_path):
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# plan\n", encoding="utf-8")
    inv = tmp_path / "state" / "mise-inventory" / "x.md"
    inv.parent.mkdir(parents=True)
    inv.write_text(_NO_OPTIONAL, encoding="utf-8")
    (minted,) = im.mint_rows(im.parse_chunk_table(_NO_OPTIONAL), inventory_path=inv)
    assert minted["id"] == "C1"
    assert "." not in minted["id"]


def test_live_spec_paths_excludes_blank():
    rows = im.parse_chunk_table(_NO_OPTIONAL)
    assert _live_spec_paths(rows) == []
    assert _has_live_row(rows) is True


def test_all_spec_less_part_is_not_skipped_but_closed_part_is():
    closed = _inv(
        "| id | summary | footprint | deps | verification | disposition |",
        "| C1 | s | `a.py` | - | v | landed |",
    )
    assert _has_live_row(im.parse_chunk_table(_NO_OPTIONAL)) is True
    assert _has_live_row(im.parse_chunk_table(closed)) is False


def test_full_column_table_body_is_unchanged():
    text = textwrap.dedent(
        """\
        ## Chunk table

        | id | spec path | summary | footprint | deps | verification | complexity | disposition |
        |---|---|---|---|---|---|---|---|
        | C1 | `docs/plans/p.md` | do it | `a.py` | - | pytest | S | pending |
        """
    )
    (minted,) = im.mint_rows(im.parse_chunk_table(text))
    assert minted["body"] == (
        "Spec: docs/plans/p.md (C1)\n"
        "do it\n"
        "Verification (this row is DONE only when this holds): pytest\n"
        "Complexity: S\n"
    )
