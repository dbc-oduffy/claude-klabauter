"""A default emit over the max_rows cap mints a tranche and records its budget and passes
remaining on the spine; `next_tranche_request` and the spent-spine re-mint read them back."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.emission_receipt import stamp_landed
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_inventory_emit_plan_edge_and_tranche_exit import (
    _BASE,
    _inventory,
    _mark_coded,
    _review_loaders,  # noqa: F401  (autouse fixture)
)
from coordinator_core.ops.dispatch_emit.tranche_chain import (
    chain_after_land,
    next_part_request,
    next_tranche_request,
    recorded_tranche_state,
)

_DEFAULTED = 6
_CAP = 5


@pytest.fixture(autouse=True)
def _small_default(monkeypatch):
    monkeypatch.setattr(op_module, "DEFAULT_TRANCHE_ROW_BUDGET", _DEFAULTED)


def _default_emit(tmp_path: Path, inventory: Path) -> dict:
    result = _dispatch_emit(
        {"inventory_path": str(inventory), "target_root": str(tmp_path), "max_rows": _CAP}
    )
    assert result["ok"] is True, result["findings"]
    return result


def _spine_of(script: Path) -> Path:
    return script.with_name(script.name.removesuffix(".workflow.mjs") + ".md")


def test_default_emit_over_cap_mints_t1_and_records_budget_and_passes(tmp_path):
    inventory = _inventory(tmp_path)

    result = _default_emit(tmp_path, inventory)

    assert Path(result["path"]).name == f"{_BASE}-t1.spine.workflow.mjs"
    assert result["tranche"]["row_budget"] == _DEFAULTED
    assert result["tranche"]["row_budget_defaulted"] is True
    assert result["tranche"]["remaining"]["passes_remaining"] > 0
    assert recorded_tranche_state(_spine_of(Path(result["path"]))) == (
        _DEFAULTED,
        result["tranche"]["remaining"]["passes_remaining"],
    )


def test_over_cap_emit_with_a_part_still_refuses(tmp_path):
    inventory = _inventory(tmp_path)

    with pytest.raises(op_module.InventoryTooLargeError):
        _dispatch_emit(
            {
                "inventory_path": str(inventory),
                "target_root": str(tmp_path),
                "max_rows": 2,  # below the part's row count, so the cap binds
                "inventory_part": [1, 2],
            }
        )


def test_next_tranche_request_names_base_record_and_recorded_budget(tmp_path):
    inventory = _inventory(tmp_path)
    script = Path(_default_emit(tmp_path, inventory)["path"])

    assert next_tranche_request(tmp_path, script) == {
        "inventory_path": str(inventory),
        "target_root": str(tmp_path),
        "row_budget": _DEFAULTED,
    }
    assert chain_after_land(tmp_path, f"{_BASE}-t1", script)["request"] == next_tranche_request(tmp_path, script)


def test_next_tranche_request_is_none_when_no_passes_remain(tmp_path):
    inventory = _inventory(tmp_path)
    result = _dispatch_emit(
        {"inventory_path": str(inventory), "target_root": str(tmp_path), "row_budget": 40}
    )

    assert result["tranche"]["remaining"]["passes_remaining"] == 0
    assert next_tranche_request(tmp_path, Path(result["path"])) is None


def test_stalled_spine_yields_no_request_and_chain_after_land_names_the_held_rows(tmp_path):
    from coordinator_core.ops.dispatch_emit.tranche_chain import CHAIN_FILE, stamp_tranche_state
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    inventory = _inventory(tmp_path)
    script = Path(_default_emit(tmp_path, inventory)["path"])
    spine = _spine_of(script)
    assert next_tranche_request(tmp_path, script) is not None
    stall = {"rows": ["A1", "A2"], "held_by": {"A1": "gate: red", "A2": "dependent of A1"}}
    text = spine.read_text(encoding="utf-8")
    old = f"tranche_row_budget: {_DEFAULTED}\ntranche_passes_remaining: {recorded_tranche_state(spine)[1]}\n"
    assert old in text
    spine.write_text(stamp_tranche_state(text.replace(old, ""), _DEFAULTED, 3, stall), encoding="utf-8")
    assert recorded_tranche_state(spine) == (_DEFAULTED, 3), "passes stay positive so only the stall stops it"

    assert "tranche_stalled:" in spine.read_text(encoding="utf-8")
    assert next_tranche_request(tmp_path, script) is None
    result = chain_after_land(tmp_path, f"{_BASE}-t1", script)
    assert result["request"] is None and result["spawned"] is False
    assert result["chain_error"] == "stalled: every row held (A1: gate: red, A2: dependent of A1)"
    record = record_dir(tmp_path, f"{_BASE}-t1") / CHAIN_FILE
    assert "stalled: every row held" in record.read_text(encoding="utf-8")


def test_next_part_request_names_the_next_part_and_none_after_the_last(tmp_path):
    inventory = _inventory(tmp_path)
    scripts = []
    for index in (1, 2):
        part = _dispatch_emit(
            {"inventory_path": str(inventory), "target_root": str(tmp_path), "inventory_part": [index, 2]}
        )
        assert part["ok"] is True, part["findings"]
        scripts.append(Path(part["path"]))

    first = next_part_request(tmp_path, scripts[0])
    assert first["inventory_part"] == [2, 2]
    assert first["inventory_path"] == str(inventory)
    assert first["target_root"] == str(tmp_path)
    assert Path(first["output_path"]).name == scripts[1].name
    assert next_part_request(tmp_path, scripts[1]) is None
    assert next_tranche_request(tmp_path, scripts[0]) is None, "a part stamp is not a tranche record"


def test_next_part_request_is_none_without_a_part_stamp(tmp_path):
    inventory = _inventory(tmp_path)
    script = Path(_default_emit(tmp_path, inventory)["path"])

    assert next_part_request(tmp_path, script) is None
    assert next_part_request(tmp_path, tmp_path / "absent.workflow.mjs") is None


def test_next_tranche_request_is_none_for_a_non_inventory_script(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("---\ntitle: x\n---\n", encoding="utf-8")

    assert next_tranche_request(tmp_path, tmp_path / "plan.workflow.mjs") is None
    assert next_tranche_request(tmp_path, tmp_path / "absent.workflow.mjs") is None


def test_spent_t1_spine_remints_t2_at_the_recorded_budget_from_the_base_record(tmp_path):
    inventory = _inventory(tmp_path)
    t1 = Path(_default_emit(tmp_path, inventory)["path"])
    stamp_landed(t1, "abc123")
    _mark_coded(tmp_path, "pred", "P", 3)
    _mark_coded(tmp_path, "dep", "D", 3)

    result = _dispatch_emit(
        {
            "plan_path": str(_spine_of(t1)),
            "output_path": str(t1),
            "target_root": str(tmp_path),
            "max_rows": _CAP,
        }
    )

    assert result["ok"] is True, result["findings"]
    assert Path(result["path"]).name == f"{_BASE}-t2.spine.workflow.mjs"
    assert result["tranche"]["row_budget"] == _DEFAULTED
    assert result["tranche"]["remaining"]["passes_remaining"] == 0
    assert next_tranche_request(tmp_path, Path(result["path"])) is None
