"""A refused inventory emit leaves no tranche record, spine or can-report-red file
on disk, so the next tranche number does not climb and script and spine agree."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.op import ForeignEmissionError, _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _repo,
    _n_rows,
)


@pytest.fixture(autouse=True)
def _review_loaders(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _inventory(tmp_path: Path) -> Path:
    return _repo(tmp_path, {"a": _n_rows("A", 2), "b": _n_rows("B", 2)}, [("a", "—"), ("b", "—")])


def _tree(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if ".git" not in p.parts}


def _refuse_params(tmp_path: Path, inventory: Path, out: Path) -> dict:
    return {
        "inventory_path": str(inventory),
        "output_path": str(out),
        "target_root": str(tmp_path),
        "row_budget": 3,
        "session_id": "refused-session",
    }


def test_refused_inventory_emit_leaves_no_residue(tmp_path):
    inventory = _inventory(tmp_path)
    out = inventory.parent / "20261008T000000-tr-t1.workflow.mjs"
    out.write_text("// someone else's script\n", encoding="utf-8")
    before = _tree(tmp_path)

    with pytest.raises(ForeignEmissionError):
        _dispatch_emit(_refuse_params(tmp_path, inventory, out))

    assert _tree(tmp_path) == before
    assert out.read_text(encoding="utf-8") == "// someone else's script\n"


def test_refused_emits_then_success_keep_script_and_spine_tranche_numbers_equal(tmp_path):
    inventory = _inventory(tmp_path)
    out = inventory.parent / "20261008T000000-tr-t1.workflow.mjs"
    out.write_text("// someone else's script\n", encoding="utf-8")
    params = _refuse_params(tmp_path, inventory, out)
    for _ in range(3):
        with pytest.raises(ForeignEmissionError):
            _dispatch_emit(params)

    out.unlink()
    result = _dispatch_emit({**params, "session_id": "landing-session"})

    assert result["ok"] is True, result["findings"]
    spines = sorted(p.name for p in inventory.parent.glob("*.spine.md"))
    assert len(spines) == 1
    spine_n = re.search(r"-t(\d+)\.spine\.md$", spines[0]).group(1)
    script_n = re.search(r"-t(\d+)\.workflow\.mjs$", out.name).group(1)
    assert spine_n == script_n == "1"
    assert not list(inventory.parent.glob("*-t[2-9].md"))
