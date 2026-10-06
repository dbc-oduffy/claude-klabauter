"""The inventory route defaults its script to `<minted-spine-stem>.workflow.mjs`
beside the minted spine; the queue route keeps requiring `--out`."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.op import ForeignEmissionError, _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_mint import (
    _WRITE_OVERLAP_INVENTORY,
    _write_inventory,
)


@pytest.fixture(autouse=True)
def _review_loaders(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _inventory(tmp_path):
    return _write_inventory(tmp_path, _WRITE_OVERLAP_INVENTORY)


def test_rpc_inventory_without_output_path_writes_script_beside_spine(tmp_path):
    inventory = _inventory(tmp_path)

    result = _dispatch_emit({"inventory_path": str(inventory), "target_root": str(tmp_path)})

    assert result["ok"] is True, result["findings"]
    spine = next(tmp_path.glob("*.spine.md"))
    script = tmp_path / (spine.name[: -len(".md")] + ".workflow.mjs")
    assert script.is_file()


def test_cli_inventory_without_out_exits_ok(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    inv_dir = repo / "state" / "mise-inventory"
    inv_dir.mkdir(parents=True)
    inventory = _write_inventory(inv_dir, _WRITE_OVERLAP_INVENTORY)
    monkeypatch.chdir(repo)

    code = cli_module.main(["--inventory", str(inventory), "--repo-root", str(repo)])

    assert code == cli_module.EXIT_OK
    assert list(inv_dir.glob("*.spine.workflow.mjs"))


def test_cli_queue_without_out_still_usage(tmp_path, capsys):
    code = cli_module.main(
        [
            "--queue", str(tmp_path / "q"),
            "--profile", "fixture",
            "--profile-dir", str(Path(__file__).parent / "fixtures" / "queue-profiles"),
            "--repo-root", str(tmp_path),
        ]
    )

    assert code == cli_module.EXIT_USAGE
    assert "--out is required" in capsys.readouterr().err


def test_second_emit_over_different_bytes_refuses(tmp_path):
    inventory = _inventory(tmp_path)
    params = {"inventory_path": str(inventory), "target_root": str(tmp_path)}
    _dispatch_emit(params)
    script = next(tmp_path.glob("*.workflow.mjs"))
    script.write_text("// someone else's script\n", encoding="utf-8")

    with pytest.raises(ForeignEmissionError):
        _dispatch_emit({**params, "session_id": "another-session"})
