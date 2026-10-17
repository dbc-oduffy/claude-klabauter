"""An inventory record must live in the repo's ``state/mise-inventory/``. A warp run's JSON
manifest is no inventory record (``mint_spine`` finds no chunk table in it), so it is refused
with the route that reviews a run's rows: ``--plan ... --review-only --rows ... --run-base``."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.op import InventoryOutsideRepoError, _refuse_inventory_outside_repo


def _record(tmp_path, *parts):
    f = tmp_path.joinpath(*parts)
    f.parent.mkdir(parents=True)
    f.write_text("x\n", encoding="utf-8")
    return str(f)


def test_a_warp_run_manifest_is_refused_naming_the_plan_route(tmp_path):
    rec = _record(tmp_path, "state", "warp", "run-1", "manifest.json")
    with pytest.raises(InventoryOutsideRepoError, match="--plan <plan.md> --review-only --rows"):
        _refuse_inventory_outside_repo(rec, tmp_path)


def test_a_record_in_mise_inventory_is_accepted(tmp_path):
    rec = _record(tmp_path, "state", "mise-inventory", "inv.md")
    _refuse_inventory_outside_repo(rec, tmp_path)


@pytest.mark.parametrize(
    "parts",
    [
        ("state", "warp", "run-1", "notes.json"),
        ("state", "warp", "manifest.json"),
        ("state", "other", "run-1", "manifest.json"),
        ("stuff", "inv.md"),
    ],
)
def test_a_record_outside_mise_inventory_is_refused(tmp_path, parts):
    rec = _record(tmp_path, *parts)
    with pytest.raises(InventoryOutsideRepoError, match="is not under"):
        _refuse_inventory_outside_repo(rec, tmp_path)
