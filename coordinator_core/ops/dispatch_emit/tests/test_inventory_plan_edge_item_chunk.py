"""An in-inventory `depends_on_plan` edge mints `<item>.<chunk>` depends_on targets
(never a bare inventory item id) across slug and `I06`-style item ids, with and
without a row budget or a part cut, and `read_spine` accepts the minted spine."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _repo,
)

_GATE = "output-consumption-runtime"
_SHARED = "src/shared_with_pred.py"


def _chain(owner: str, n: int, disposition: str = "open") -> list:
    rows = []
    for k in range(1, n + 1):
        extra = {"disposition": disposition}
        if k > 1:
            extra["depends_on"] = [{"chunk": f"C{k - 1}", "gate_kind": _GATE}]
        rows.append(_chunk(f"C{k}", f"src/{owner}_C{k}.py", **extra))
    return rows


def _fixture(tmp_path, pred_item: str, dep_item: str, *, pred_disposition="open", dep_first=False):
    """Predecessor plan (6 chunks) and dependent plan (7 chunks) whose C7 carries a chunk-form
    `depends_on_plan` edge onto the predecessor's C6 and also writes `_SHARED`, which the
    predecessor item's Chunk-table footprint names."""
    pred = _chain(pred_item, 6, pred_disposition)
    dep = _chain(dep_item, 7)
    dep[-1]["writes"] = [f"src/{dep_item}_C7.py", _SHARED]
    dep[-1]["depends_on_plan"] = [
        {"plan": f"docs/plans/{pred_item}.md", "chunk": "C6", "gate_kind": _GATE}
    ]
    table = [(pred_item, "—"), (dep_item, "—")]
    if dep_first:
        table.reverse()
    inv = _repo(tmp_path, {pred_item: pred, dep_item: dep}, table)
    text = inv.read_text(encoding="utf-8")
    inv.write_text(
        text.replace(f"| s {pred_item} | `docs/plans/{pred_item}.md` |", f"| s {pred_item} | `{_SHARED}` |"),
        encoding="utf-8",
    )
    return inv


def _spine_rows(text: str) -> list:
    return yaml.safe_load(text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0])


def _assert_item_chunk_edges(spine_text: str, spine_path) -> list:
    rows = _spine_rows(spine_text)
    ids = {r["id"] for r in rows}
    for row in rows:
        for edge in row.get("depends_on", []):
            assert "." in edge["chunk"], (row["id"], edge)
            assert edge["chunk"] in ids, (row["id"], edge)
    spine_path.parent.mkdir(parents=True, exist_ok=True)
    spine_path.write_text(spine_text, encoding="utf-8")
    assert {r.id for r in read_spine(spine_path)} == ids
    return rows


_IDS = [
    ("claude-usage-report-text-ingest", "example-fleet-context-publish-pipeline"),
    ("I06", "I13"),
]


@pytest.mark.parametrize("pred_item,dep_item", _IDS)
@pytest.mark.parametrize("dep_first", [False, True])
@pytest.mark.parametrize("budget", [None, 40, 13])
def test_open_predecessor_chunk_mints_item_dot_chunk(tmp_path, pred_item, dep_item, dep_first, budget):
    inv = _fixture(tmp_path, pred_item, dep_item, dep_first=dep_first)
    text, spine_path = im.mint_spine(str(inv), row_budget=budget)

    rows = _assert_item_chunk_edges(text, tmp_path / "scratch" / spine_path.name)

    by_id = {r["id"]: r for r in rows}
    edges = [e["chunk"] for e in by_id[f"{dep_item}.C7"]["depends_on"]]
    assert f"{pred_item}.C6" in edges


@pytest.mark.parametrize("pred_item,dep_item", _IDS)
@pytest.mark.parametrize("budget", [None, 40])
def test_fully_landed_predecessor_leaves_no_bare_item_edge(tmp_path, pred_item, dep_item, budget):
    """A predecessor item whose plan chunks are all closed mints no row, so a
    write overlap with it must not surface as an edge onto the bare item id."""
    inv = _fixture(tmp_path, pred_item, dep_item, pred_disposition="coded")
    text, spine_path = im.mint_spine(str(inv), row_budget=budget)

    rows = _assert_item_chunk_edges(text, tmp_path / "scratch" / spine_path.name)

    by_id = {r["id"]: r for r in rows}
    assert {r["id"].split(".", 1)[0] for r in rows} == {dep_item}
    assert pred_item not in {e["chunk"] for e in by_id[f"{dep_item}.C7"].get("depends_on", [])}


@pytest.mark.parametrize("pred_item,dep_item", _IDS)
def test_tight_budget_defers_the_dependent_with_its_predecessor_only(tmp_path, pred_item, dep_item):
    inv = _fixture(tmp_path, pred_item, dep_item)
    report: dict = {}
    text, spine_path = im.mint_spine(str(inv), row_budget=6, tranche_out=report)

    rows = _assert_item_chunk_edges(text, tmp_path / "scratch" / spine_path.name)

    assert report["plans"] == [pred_item]
    assert {r["id"].split(".", 1)[0] for r in rows} == {pred_item}


@pytest.mark.parametrize("pred_item,dep_item", _IDS)
@pytest.mark.parametrize("index", [1, 2])
def test_part_cut_never_leaves_a_bare_item_edge(tmp_path, pred_item, dep_item, index):
    inv = _fixture(tmp_path, pred_item, dep_item)
    text, _ = im.mint_spine(str(inv), part=(index, 2))

    for row in _spine_rows(text):
        for edge in row.get("depends_on", []):
            assert edge["chunk"] not in (pred_item, dep_item), (row["id"], edge)


def test_edge_to_a_pred_chunk_outside_the_run_still_withholds(tmp_path):
    """Anti-scope: an edge onto a plan that is not an inventory item is never rewritten
    to a bare item id (asserts the no-bare-edge half only; withholding is not asserted)."""
    pred, dep = "pred-plan", "dep-plan"
    inv = _fixture(tmp_path, pred, dep)
    text = inv.read_text(encoding="utf-8")
    inv.write_text("\n".join(l for l in text.splitlines() if not l.startswith(f"| {pred} ")) + "\n", encoding="utf-8")
    withheld: dict = {}
    rows = im.mint_rows(
        im.parse_chunk_table(inv.read_text(encoding="utf-8")), inventory_path=inv, withheld_out=withheld
    )

    assert all(e["chunk"] != pred for r in rows for e in r.get("depends_on", []))
