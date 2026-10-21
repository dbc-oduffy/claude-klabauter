"""A tranche emit never exits on "depends_on unresolvable chunk" for a
`depends_on_plan` edge onto an in-inventory chunk that is not in the emitted
set: the dependent defers; a chunk of no inventory item still refuses."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.spine_read import DanglingDependencyError, read_spine
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _repo,
)

_GATE = "output-consumption-runtime"


def _edge(plan: str, chunk: str) -> list:
    return [{"plan": f"docs/plans/{plan}.md", "chunk": chunk, "gate_kind": _GATE}]


def _plans() -> dict:
    """`pred` C1 waits on `ext` (outside the inventory), so it is held while C2 is
    minted; `dep` C1 waits on `pred`.C1."""
    return {
        "ext": [_chunk("E1", "src/e1.py")],
        "pred": [
            _chunk("C1", "src/p1.py", depends_on_plan=_edge("ext", "E1")),
            _chunk("C2", "src/p2.py"),
        ],
        "dep": [_chunk("C1", "src/d1.py", depends_on_plan=_edge("pred", "C1"))],
        "free": [_chunk("F1", "src/f1.py")],
    }


def _tranche(inv, budget):
    report: dict = {}
    text, spine_path = im.mint_spine(str(inv), row_budget=budget, tranche_out=report, accounting_out={})
    rows = yaml.safe_load(text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0])
    return text, spine_path, rows, report


@pytest.mark.parametrize("budget", [1, 2, 3, 100])
def test_edge_onto_unminted_chunk_of_taken_item_defers_dependent(tmp_path, budget):
    inv = _repo(tmp_path, _plans(), [("pred", "—"), ("dep", "—"), ("free", "—")])
    text, spine_path, rows, report = _tranche(inv, budget)
    ids = {r["id"] for r in rows}
    assert "pred.C1" not in ids
    assert "dep" not in report["plans"], "dep taken although pred.C1 is not emitted"
    assert "dep.C1" not in ids
    if budget >= 2:
        assert "pred.C2" in ids
        assert any(d["plan"] == "dep" for d in report["deferred"])
    spine_path.parent.mkdir(parents=True, exist_ok=True)
    spine_path.write_text(text, encoding="utf-8")
    assert {r.id for r in read_spine(spine_path)} == ids


def test_edge_onto_chunk_of_no_inventory_item_still_refuses(tmp_path):
    rows = [
        {"id": "a.C1", "title": "t", "surface": "s.py", "writes": ["s.py"], "change_kind": "code-edit",
         "body": "b", "depends_on": [{"chunk": "ghost.C1", "gate_kind": _GATE}]},
    ]
    selected, report = im.select_tranche(rows, ["a"], 10)
    assert report["plans"] == ["a"]
    spine = tmp_path / "x.spine.md"
    spine.write_text(
        "## Tasks\n\n```yaml plan-tasks\n" + yaml.safe_dump(selected, sort_keys=False) + "```\n",
        encoding="utf-8",
    )
    with pytest.raises(DanglingDependencyError, match="ghost.C1"):
        read_spine(spine)
