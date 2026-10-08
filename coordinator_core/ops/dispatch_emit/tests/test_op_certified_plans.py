from __future__ import annotations

from coordinator_core.ops.dispatch_emit.op import _certified_plans

_TABLE = (
    "---\nrun_id: r\n---\n\n## Chunk table\n\n"
    "| id | summary | footprint | deps | verification | disposition | spec path |\n"
    "|---|---|---|---|---|---|---|\n"
    "{rows}\n"
)
_ROW = "| {id} | s | f | - | v | live | `{spec}` |"


def _inv(path, specs):
    rows = "\n".join(_ROW.format(id=f"c{i}", spec=s) for i, s in enumerate(specs))
    path.write_text(_TABLE.format(rows=rows), encoding="utf-8")
    return path


def test_a_tranche_answers_for_its_base_inventorys_full_set(tmp_path):
    _inv(tmp_path / "run.md", ["docs/plans/a.md", "docs/plans/b.md", "docs/plans/c.md"])
    tranche = _inv(tmp_path / "run-t2.md", ["docs/plans/a.md"])
    assert _certified_plans(str(tranche)) == ["docs/plans/a.md", "docs/plans/b.md", "docs/plans/c.md"]


def test_a_main_inventory_answers_for_itself(tmp_path):
    inv = _inv(tmp_path / "run.md", ["docs/plans/b.md", "docs/plans/a.md"])
    assert _certified_plans(str(inv)) == ["docs/plans/a.md", "docs/plans/b.md"]


def test_no_inventory_or_unreadable_table_is_empty(tmp_path):
    assert _certified_plans(None) == []
    bad = tmp_path / "x.md"
    bad.write_text("no table\n", encoding="utf-8")
    assert _certified_plans(str(bad)) == []
