"""Exit-criterion falsifier: a real `_dispatch_emit` over two in-inventory plans joined by a
chunk-form `depends_on_plan` edge mints `<item>.<chunk>` deps, and a later `--row-budget`
emit over a dir already holding `-t1` outputs mints `-t2` and leaves every `-t1` script,
spine and receipt byte-identical."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path, stamp_landed
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _n_rows,
    _plan_text,
    _repo,
)

_GATE = "output-consumption-runtime"
_BASE = "20261008T000000-tr"
_BUDGET = 6


@pytest.fixture(autouse=True)
def _review_loaders(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _inventory(tmp_path: Path) -> Path:
    """`pred` (3 rows) and `dep` (3 rows, its D2 gated on pred's P2 by a chunk-form plan edge)
    fill the budget; `late` (3 rows) is deferred to the next tranche."""
    pred = _n_rows("P", 3)
    dep = _n_rows("D", 3)
    dep[1]["depends_on_plan"] = [{"plan": "docs/plans/pred.md", "chunk": "P2", "gate_kind": _GATE}]
    return _repo(
        tmp_path,
        {"pred": pred, "dep": dep, "late": _n_rows("L", 3)},
        [("pred", "—"), ("dep", "—"), ("late", "—")],
    )


def _emit(tmp_path: Path, inventory: Path) -> dict:
    result = _dispatch_emit(
        {"inventory_path": str(inventory), "target_root": str(tmp_path), "row_budget": _BUDGET}
    )
    assert result["ok"] is True, result["findings"]
    return result


def _t1_files(tmp_path: Path) -> "dict[str, bytes]":
    inv_dir = tmp_path / "state" / "mise-inventory"
    return {
        p.name: p.read_bytes()
        for p in sorted(inv_dir.iterdir())
        if re.search(r"-t1(\.|-)", p.name)
    }


def _mark_coded(tmp_path: Path, name: str, prefix: str, n: int) -> None:
    rows = [_chunk(f"{prefix}{k}", f"src/{prefix}{k}.py", disposition="coded") for k in range(n)]
    (tmp_path / "docs" / "plans" / f"{name}.md").write_text(_plan_text(rows), encoding="utf-8")


def _first_emit(tmp_path: Path) -> "tuple[Path, Path]":
    inventory = _inventory(tmp_path)
    result = _emit(tmp_path, inventory)
    script = Path(result["path"])
    spine = script.with_name(script.name.removesuffix(".workflow.mjs").removesuffix(".spine") + ".spine.md")
    assert f"{_BASE}-t1" in script.name

    rows = yaml.safe_load(spine.read_text(encoding="utf-8").split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0])
    ids = {r["id"] for r in rows}
    assert ids == {f"{p}.{c}" for p, ch in (("pred", "P"), ("dep", "D")) for c in (f"{ch}0", f"{ch}1", f"{ch}2")}
    edges = {e["chunk"] for r in rows for e in r.get("depends_on", [])}
    assert "pred.P2" in edges
    for r in rows:
        for e in r.get("depends_on", []):
            assert "." in e["chunk"] and e["chunk"] in ids, (r["id"], e)
    assert {r.id for r in read_spine(spine)} == ids
    return inventory, script


def _assert_second_emit_mints_t2(tmp_path: Path, inventory: Path) -> None:
    before = _t1_files(tmp_path)
    assert before
    _mark_coded(tmp_path, "pred", "P", 3)
    _mark_coded(tmp_path, "dep", "D", 3)

    result = _emit(tmp_path, inventory)

    assert f"{_BASE}-t2" in Path(result["path"]).name
    assert (tmp_path / "state" / "mise-inventory" / f"{_BASE}-t2.spine.md").is_file()
    assert _t1_files(tmp_path) == before


def test_plan_edge_mints_item_chunk_and_landed_t1_survives_t2_emit(tmp_path):
    inventory, script = _first_emit(tmp_path)
    assert emission_receipt_path(script).is_file()
    stamp_landed(script, "abc123")
    assert "landed" in emission_receipt_path(script).read_text(encoding="utf-8")

    _assert_second_emit_mints_t2(tmp_path, inventory)


def test_unlanded_t1_with_resume_sibling_survives_t2_emit(tmp_path):
    inventory, script = _first_emit(tmp_path)
    resume = script.with_name(script.name.removesuffix(".workflow.mjs") + "-resume.workflow.mjs")
    resume.write_text("// resumed run\n", encoding="utf-8")
    assert "landed" not in emission_receipt_path(script).read_text(encoding="utf-8")

    _assert_second_emit_mints_t2(tmp_path, inventory)
