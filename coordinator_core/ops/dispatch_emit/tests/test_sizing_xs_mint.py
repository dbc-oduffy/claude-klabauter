"""Tests for the XS arm spine mint."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine
from coordinator_core.ops.dispatch_emit.spine_read import read_spine

SIZING_REL = "state/sizings/2026-10-01-xs-thing.yaml"
SIZING = {
    "deliverable_id": "dlv-xs-thing-abc123",
    "intent": "Fix the thing\nmore detail here",
    "exit_criterion": {"statement": "the thing passes: 'quoted' text", "accepted": {"pm_quote": "go"}},
}


def test_mint_round_trips_to_one_dispatchable_row(tmp_path):
    text, path = mint_xs_spine(
        SIZING, sizing_rel=SIZING_REL, writes=["a/b.py", "a/test_b.py"], out_dir=tmp_path
    )
    assert path == tmp_path / "2026-10-01-xs-thing.spine.md"
    path.write_text(text, encoding="utf-8", newline="\n")
    rows = read_spine(path)
    assert len(rows) == 1
    assert rows[0].id == "X1"
    assert list(rows[0].writes) == ["a/b.py", "a/test_b.py"]
    assert "the thing passes" in rows[0].body


def test_frontmatter_cites_sizing_and_statement(tmp_path):
    text, _ = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.md"], out_dir=tmp_path)
    assert f"sizing_object: {SIZING_REL}" in text
    assert "deliverable_id: dlv-xs-thing-abc123" in text
    assert "run_id: 2026-10-01-xs-thing" in text
    assert "the thing passes: 'quoted' text" in text


def test_deliverable_id_omitted_when_absent(tmp_path):
    sizing = {k: v for k, v in SIZING.items() if k != "deliverable_id"}
    text, _ = mint_xs_spine(sizing, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path)
    assert "deliverable_id" not in text


def test_glob_writes_refused(tmp_path):
    with pytest.raises(im.GlobFootprintError):
        mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a/*.py"], out_dir=tmp_path)


def test_directory_writes_refused(tmp_path):
    with pytest.raises(im.DirectoryShapedFootprintError):
        mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a/dir/"], out_dir=tmp_path)


def test_empty_writes_refused(tmp_path):
    with pytest.raises(ValueError):
        mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=[], out_dir=tmp_path)


def test_minted_spine_derives_lightweight_review_tier(tmp_path):
    from coordinator_core.ops.dispatch_emit.emit import derive_review_tier

    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text("estimate:\n  tshirt: XS\n", encoding="utf-8")
    text, path = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path)
    assert derive_review_tier(path, repo_root=tmp_path, plan_text=text) == "lightweight"


def test_declared_gated_row_is_minted_withheld_never_dropped(tmp_path):
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import gated_rows
    from coordinator_core.ops.dispatch_emit.spine_read import load_rows

    gated = [{"title": "DoE dead-op prose", "owner_repo": "coordinator-content-repo", "requires": "landed-work"}]
    text, path = mint_xs_spine(
        SIZING, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path, gated=gated
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    exclusions: list = []
    rows = read_spine(path, exclusions=exclusions)
    assert [r.id for r in rows] == ["X1"]
    raw = {r["id"]: r for r in load_rows(text).rows}
    withheld = gated_rows(exclusions, raw)
    assert [(g.id, g.reason, g.owner_repo) for g in withheld] == [("X2", "external_gate", "coordinator-content-repo")]
