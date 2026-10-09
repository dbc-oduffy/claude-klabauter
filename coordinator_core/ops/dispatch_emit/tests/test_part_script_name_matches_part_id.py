"""An over-cap inventory's part scripts are named by the same run id the part's spine carries."""
from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import inventory_mint as im


def test_part_script_name_carries_exactly_the_spine_part_id(tmp_path, monkeypatch):
    tranche = tmp_path / "run7-t3.md"
    tranche.write_text("---\nrun_id: run7-t3\n---\n", encoding="utf-8")
    seen = []

    def fake_emit(params, repo_root=None):
        seen.append(params)
        return {"ok": True, "path": params["output_path"]}

    monkeypatch.setattr(cli_module, "_dispatch_emit", fake_emit)
    over = SimpleNamespace(script_size=3_000_000, row_count=9, tranche_inventory=str(tranche))
    over.script_size = cli_module._emit._WORKFLOW_SCRIPT_BYTE_CAP * 2
    cli_module._emit_inventory_parts(
        {"inventory_path": str(tmp_path / "run7.md")}, None, over, False, {}
    )
    assert len(seen) >= 2
    for part in seen:
        index = part["inventory_part"][0]
        part_id = im.part_run_id("run7-t3", index)
        name = part["output_path"].rsplit("/", 1)[-1]
        assert name == f"{part_id}.workflow.mjs"
