"""`op._refuse_spent_overwrite` refuses to change an emitted script or spine whose run id has
landed or run, and lets unspent or byte-identical targets through; the two real write sites
(`_emit_script_file`, `_dispatch_emit`'s spine write) honour it even with `force` set."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path, stamp_landed
from coordinator_core.ops.dispatch_emit.tests.test_inventory_emit_plan_edge_and_tranche_exit import (
    _BASE,
    _BUDGET,
    _first_emit,
    _review_loaders,  # noqa: F401  (autouse fixture)
)

_INV_DIR = ("state", "mise-inventory")


def _script(tmp_path, name="run-t1-p1"):
    script = tmp_path / f"{name}.workflow.mjs"
    script.write_text("old", encoding="utf-8")
    return script


def test_landed_script_refuses_changed_bytes_naming_evidence(tmp_path):
    script = _script(tmp_path)
    (tmp_path / (script.name + ".emitted.json")).write_text(
        json.dumps({"sha256": "a", "landed": {"sha": "abc", "at": "t"}}), encoding="utf-8"
    )
    with pytest.raises(op_module.SpentRunOverwriteError) as exc:
        op_module._refuse_spent_overwrite(script, b"new", script)
    assert ".emitted.json" in str(exc.value)
    assert "--row-budget" in str(exc.value)
    assert script.read_text(encoding="utf-8") == "old"


def test_resume_sibling_spends_the_spine_too(tmp_path):
    script = _script(tmp_path)
    (tmp_path / "run-t1-p1-resume.workflow.mjs").write_text("r", encoding="utf-8")
    spine = tmp_path / "run-t1-p1.spine.md"
    spine.write_text("old spine", encoding="utf-8")
    with pytest.raises(op_module.SpentRunOverwriteError) as exc:
        op_module._refuse_spent_overwrite(spine, b"new spine", script)
    assert "run-t1-p1-resume.workflow.mjs" in str(exc.value)


def test_identical_bytes_pass(tmp_path):
    script = _script(tmp_path)
    (tmp_path / "run-t1-p1-land.workflow.mjs").write_text("l", encoding="utf-8")
    op_module._refuse_spent_overwrite(script, b"old", script)


def test_unlanded_receipt_without_evidence_passes(tmp_path):
    script = _script(tmp_path)
    (tmp_path / (script.name + ".emitted.json")).write_text(json.dumps({"sha256": "a"}), encoding="utf-8")
    op_module._refuse_spent_overwrite(script, b"new", script)


def test_missing_target_passes(tmp_path):
    script = tmp_path / "run-t1.workflow.mjs"
    op_module._refuse_spent_overwrite(script, b"new", script)


def _emit_part(tmp_path, part):
    return op_module._dispatch_emit(
        {
            "inventory_path": str(tmp_path.joinpath(*_INV_DIR, "inv-t1.md")),
            "target_root": str(tmp_path),
            "row_budget": _BUDGET,
            "inventory_part": part,
            "force": True,
        }
    )


def test_emit_script_file_refuses_spent_path_even_with_force(tmp_path, monkeypatch):
    monkeypatch.setattr(op_module, "check_agent_types_resolve", lambda *a, **k: None)
    monkeypatch.setattr(op_module, "run_checks", lambda script: [])
    script = _script(tmp_path)
    evidence = tmp_path / "run-t1-p1-resume.workflow.mjs"
    evidence.write_text("r", encoding="utf-8")
    with pytest.raises(op_module.SpentRunOverwriteError) as exc:
        op_module._emit_script_file(
            script,
            "new script",
            receipt_plan_path=None,
            params={"force": True},
            receipt_extras=None,
            plan_findings=[],
            session_id="s",
            agent_type_host=None,
            before_write=lambda: None,
        )
    assert evidence.name in str(exc.value)
    assert script.read_text(encoding="utf-8") == "old"


def test_dispatch_emit_refuses_spent_spine_path_even_with_force(tmp_path):
    _first_emit(tmp_path)
    inv_dir = tmp_path.joinpath(*_INV_DIR)
    assert _emit_part(tmp_path, [1, 2])["ok"] is True
    p1_script = inv_dir / f"{_BASE}-t1-p1.spine.workflow.mjs"
    p1_spine = inv_dir / f"{_BASE}-t1-p1.spine.md"
    evidence = inv_dir / f"{_BASE}-t1-p1.spine-resume.workflow.mjs"
    evidence.write_text("// resumed\n", encoding="utf-8")
    p1_spine.write_text(p1_spine.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
    spine_before = p1_spine.read_bytes()
    script_before = p1_script.read_bytes()

    with pytest.raises(op_module.SpentRunOverwriteError) as exc:
        _emit_part(tmp_path, [1, 2])

    assert evidence.name in str(exc.value)
    assert p1_spine.read_bytes() == spine_before
    assert p1_script.read_bytes() == script_before


def test_recut_of_spent_part_refused_and_unspent_sibling_emits(tmp_path):
    _first_emit(tmp_path)
    inv_dir = tmp_path.joinpath(*_INV_DIR)
    assert _emit_part(tmp_path, [1, 2])["ok"] is True
    p1_script = inv_dir / f"{_BASE}-t1-p1.spine.workflow.mjs"
    p1_script.write_text(p1_script.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")
    stamp_landed(p1_script, "abc123")
    assert "landed" in emission_receipt_path(p1_script).read_text(encoding="utf-8")
    p1_script_before = p1_script.read_bytes()
    p1_spine = inv_dir / f"{_BASE}-t1-p1.spine.md"
    p1_spine.write_text(p1_spine.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
    spine_before = p1_spine.read_bytes()

    with pytest.raises(op_module.SpentRunOverwriteError):
        _emit_part(tmp_path, [1, 2])
    assert p1_spine.read_bytes() == spine_before
    assert p1_script.read_bytes() == p1_script_before

    sibling = _emit_part(tmp_path, [2, 2])
    assert sibling["ok"] is True
    assert re.search(rf"{re.escape(_BASE)}-t1-p2\.", Path(sibling["path"]).name)
    assert not (inv_dir / f"{_BASE}-t2.spine.md").exists()
