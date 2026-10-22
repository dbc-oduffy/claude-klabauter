"""Inventory emit artifacts: one naming scheme, no tranche record when the budget does not
bind, an --out a split does not honour is reported, and a re-emit leaves no stale sibling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import emit as emit_module
from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.session.record_homes import home_dir

_RUN_ID = "20261019T000000-naming"
_ITEMS = 8


@pytest.fixture(autouse=True)
def _review_inputs(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _repo_with_inventory(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    lines = [
        f"| I{i} | `docs/plans/p{i}.md` | item {i} | `src/mod_{i}.py` | — | scoped pytest | S | queued |"
        for i in range(1, _ITEMS + 1)
    ]
    text = (
        f"---\nrun_id: {_RUN_ID}\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(lines) + "\n"
    )
    inventory = inv_dir / f"{_RUN_ID}.md"
    inventory.write_text(text, encoding="utf-8")
    return inventory


def _argv(tmp_path: Path, inventory: Path, *extra: str, out: str | None = None) -> list:
    argv = ["--inventory", str(inventory), "--repo-root", str(tmp_path), *extra]
    if out:
        argv += ["--out", str(tmp_path / out)]
    return argv


def _cap_that_splits(tmp_path: Path, inventory: Path, monkeypatch) -> int:
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    op_mod._dispatch_emit(
        {"inventory_path": str(inventory), "output_path": str(tmp_path / "whole.mjs"), "target_root": str(tmp_path)},
        repo_root=tmp_path,
    )
    op_mod._dispatch_emit(
        {
            "inventory_path": str(inventory),
            "output_path": str(tmp_path / "half.mjs"),
            "inventory_part": [1, 2],
            "target_root": str(tmp_path),
        },
        repo_root=tmp_path,
    )
    whole = (tmp_path / "whole.mjs").stat().st_size
    half = (tmp_path / "half.mjs").stat().st_size
    for scratch in ("whole.mjs", "half.mjs"):
        (tmp_path / scratch).unlink()
    for leftover in inventory.parent.glob("*.spine.md"):
        leftover.unlink()
    return half + (whole - half) // 4


def test_non_binding_budget_writes_no_tranche_record(tmp_path):
    inventory = _repo_with_inventory(tmp_path)
    sink: dict = {}

    _, spine = im.mint_spine(str(inventory), row_budget=100, tranche_inventory_out=sink)

    assert sink == {}
    assert spine.name == f"{_RUN_ID}.spine.md"


def test_binding_budget_names_record_spine_and_run_id_by_one_tranche_stem(tmp_path):
    inventory = _repo_with_inventory(tmp_path)
    sink: dict = {}

    text, spine = im.mint_spine(str(inventory), row_budget=3, tranche_inventory_out=sink)

    assert sink["path"].name == f"{_RUN_ID}-t1.md"
    assert spine.name == f"{_RUN_ID}-t1.spine.md"
    assert f"run_id: {_RUN_ID}-t1\n" in text


def test_part_script_and_spine_share_the_run_id_stem(tmp_path):
    spine = im.spine_path_for(tmp_path / "x.md", im.part_run_id("r-t2", 1))
    assert spine.name.removesuffix(".spine.md") == im.part_script_name("r-t2", 1).removesuffix(".workflow.mjs")


def test_split_with_out_reports_it_and_names_parts_by_run_id(tmp_path, monkeypatch, capsys):
    inventory = _repo_with_inventory(tmp_path)
    cap = _cap_that_splits(tmp_path, inventory, monkeypatch)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", cap)
    capsys.readouterr()

    rc = cli_module.main(_argv(tmp_path, inventory, out="chosen.workflow.mjs"))

    captured = capsys.readouterr()
    assert rc == cli_module.EXIT_OK
    reply = json.loads(captured.out)
    assert not (tmp_path / "chosen.workflow.mjs").exists()
    assert reply["out_ignored"]["requested"] == str(tmp_path / "chosen.workflow.mjs")
    assert "chosen.workflow.mjs was not written" in captured.err
    for index in (1, 2):
        assert (tmp_path / im.part_script_name(_RUN_ID, index)).is_file()
        assert (inventory.parent / f"{im.part_run_id(_RUN_ID, index)}.spine.md").is_file()


def test_split_without_out_reports_no_ignored_out(tmp_path, monkeypatch, capsys):
    inventory = _repo_with_inventory(tmp_path)
    cap = _cap_that_splits(tmp_path, inventory, monkeypatch)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", cap)
    capsys.readouterr()

    assert cli_module.main(_argv(tmp_path, inventory)) == cli_module.EXIT_OK

    assert "out_ignored" not in json.loads(capsys.readouterr().out)


def test_reemit_after_split_removes_the_stale_parts_and_pre_split_spine(tmp_path, monkeypatch, capsys):
    inventory = _repo_with_inventory(tmp_path)
    cap = _cap_that_splits(tmp_path, inventory, monkeypatch)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", cap)
    assert cli_module.main(_argv(tmp_path, inventory)) == cli_module.EXIT_OK
    stale = [inventory.parent / f"{im.part_run_id(_RUN_ID, i)}.spine.md" for i in (1, 2)]
    assert all(p.is_file() for p in stale)
    # Cleanup keeps any part whose receipt exists (landed or not); only receipt-less leftovers go.
    for receipt in inventory.parent.glob("*-p*.workflow.mjs.emitted.json"):
        receipt.unlink()
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    capsys.readouterr()

    assert cli_module.main(_argv(tmp_path, inventory)) == cli_module.EXIT_OK

    assert not any(p.exists() for p in stale)
    assert not list(inventory.parent.glob("*-p*.workflow.mjs"))
    assert not list(inventory.parent.glob("*-p*.spine.md"))
    assert (inventory.parent / f"{_RUN_ID}.spine.md").is_file()
    assert "removed stale earlier output" in capsys.readouterr().err


def test_reemit_never_touches_another_run_id_or_a_tranche_record(tmp_path, monkeypatch, capsys):
    inventory = _repo_with_inventory(tmp_path)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    other = inventory.parent / f"{_RUN_ID}x-p1.spine.md"
    other.write_text("other run\n", encoding="utf-8")
    assert cli_module.main(_argv(tmp_path, inventory, "--row-budget", "3")) == cli_module.EXIT_OK
    record = inventory.parent / f"{_RUN_ID}-t1.md"
    assert record.is_file()
    capsys.readouterr()

    assert cli_module.main(_argv(tmp_path, inventory, "--row-budget", "100")) == cli_module.EXIT_OK

    assert other.is_file()
    assert record.is_file()
    err = capsys.readouterr().err
    assert str(record) in err
    # The tranche's script is regenerable and goes; its spine has no script and no landed stamp, and
    # no live part supersedes it, so it is kept and surfaced.
    assert not (inventory.parent / f"{_RUN_ID}-t1.spine.workflow.mjs").exists()
    assert (inventory.parent / f"{_RUN_ID}-t1.spine.md").is_file()
    assert f"kept unlanded spine {inventory.parent / f'{_RUN_ID}-t1.spine.md'}" in err
