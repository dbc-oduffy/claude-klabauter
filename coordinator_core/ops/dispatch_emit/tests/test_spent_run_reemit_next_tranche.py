"""A spent run id (its script carries a landed stamp) is continued at the family's next tranche by
both re-emit routes -- `--inventory <base record> --row-budget N` and `--plan <spine> --only-incomplete`
-- and a refused emit leaves the inventory directory byte-identical."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.emission_receipt import stamp_landed
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_inventory_emit_plan_edge_and_tranche_exit import (
    _BASE,
    _inventory,
    _review_loaders,  # noqa: F401  (autouse fixture)
)

_ROOMY = 40


def _inv_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "mise-inventory"


def _snapshot(tmp_path: Path) -> "dict[str, bytes]":
    return {p.name: p.read_bytes() for p in sorted(_inv_dir(tmp_path).iterdir()) if p.is_file()}


def _default_out(spine: Path) -> str:
    """The `--out` the CLI defaults to for a plan: `<plan stem>.workflow.mjs` beside it."""
    return str(spine.with_name(f"{spine.stem}.workflow.mjs"))


def _spent_base(tmp_path: Path) -> "tuple[Path, Path]":
    """A base-run emit whose script is stamped landed: (inventory record, minted spine)."""
    inventory = _inventory(tmp_path)
    result = _dispatch_emit(
        {"inventory_path": str(inventory), "target_root": str(tmp_path), "row_budget": _ROOMY}
    )
    assert result["ok"] is True
    script = Path(result["path"])
    assert script.name == f"{_BASE}.spine.workflow.mjs"
    stamp_landed(script, "abc123")
    return inventory, script.with_name(f"{_BASE}.spine.md")


def test_inventory_route_row_budget_mints_next_tranche_when_base_is_spent(tmp_path):
    inventory, _ = _spent_base(tmp_path)
    before = _snapshot(tmp_path)

    result = _dispatch_emit(
        {"inventory_path": str(inventory), "target_root": str(tmp_path), "row_budget": _ROOMY}
    )

    assert result["ok"] is True
    assert Path(result["path"]).name == f"{_BASE}-t1.spine.workflow.mjs"
    assert (_inv_dir(tmp_path) / f"{_BASE}-t1.spine.md").is_file()
    after = _snapshot(tmp_path)
    assert {k: after[k] for k in before if k in after and k.startswith(_BASE + ".")} == {
        k: v for k, v in before.items() if k.startswith(_BASE + ".")
    }


def test_plan_route_only_incomplete_mints_next_tranche_when_spine_is_spent(tmp_path):
    _, spine = _spent_base(tmp_path)

    result = _dispatch_emit(
        {
            "plan_path": str(spine),
            "output_path": _default_out(spine),
            "target_root": str(tmp_path),
            "landed_rows": ["pred.P0", "pred.P1"],
            "auto_split": True,
        }
    )

    assert result["ok"] is True
    assert Path(result["path"]).name == f"{_BASE}-t1.spine.workflow.mjs"
    minted = (_inv_dir(tmp_path) / f"{_BASE}-t1.spine.md").read_text(encoding="utf-8")
    assert "pred.P2" in minted
    script = Path(result["path"]).read_text(encoding="utf-8")
    assert "late.L0" in script


def test_refused_plan_route_emit_writes_nothing_and_names_a_working_route(tmp_path):
    _, spine = _spent_base(tmp_path)
    before = _snapshot(tmp_path)

    with pytest.raises(op_module.SpentRunOverwriteError) as exc:
        _dispatch_emit(
            {
                "plan_path": str(spine),
                "output_path": _default_out(spine),
                "target_root": str(tmp_path),
                "hold_rows": ["pred.P0"],
                "hold_reason": "waiting",
            }
        )

    assert "--row-budget" not in str(exc.value)
    assert "output_path" in str(exc.value)
    assert _snapshot(tmp_path) == before


def test_refusal_hint_for_plan_route_output_path_works(tmp_path):
    _, spine = _spent_base(tmp_path)
    other = _inv_dir(tmp_path) / "elsewhere.workflow.mjs"

    result = _dispatch_emit(
        {
            "plan_path": str(spine),
            "target_root": str(tmp_path),
            "output_path": str(other),
            "hold_rows": ["pred.P0"],
            "hold_reason": "waiting",
        }
    )

    assert result["ok"] is True
    assert other.is_file()


def test_refused_part_recut_writes_nothing_and_does_not_name_row_budget_alone(tmp_path):
    inventory = _inventory(tmp_path)
    first = _dispatch_emit(
        {"inventory_path": str(inventory), "target_root": str(tmp_path), "row_budget": 6}
    )
    t1 = Path(first["path"])
    stamp_landed(t1, "abc123")
    before = _snapshot(tmp_path)

    # A non-lane part recut is exercised through the spine-site refusal directly.
    spine = t1.with_name(t1.name.removesuffix(".spine.workflow.mjs").removesuffix(".workflow.mjs") + ".spine.md")
    with pytest.raises(op_module.SpentRunOverwriteError) as exc:
        op_module._refuse_spent_overwrite(
            spine, b"changed", t1, op_module._SPENT_HINT_PART
        )

    assert "without --part" in str(exc.value)
    assert _snapshot(tmp_path) == before
