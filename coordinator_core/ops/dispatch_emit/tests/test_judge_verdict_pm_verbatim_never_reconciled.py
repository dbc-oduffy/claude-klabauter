"""The not_met-to-met reconcile never flips a verdict whose withheld clause names a PM-verbatim row."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.emit import (
    _JUDGE_VERDICT_RULES_JS,
    _never_stranding_criterion,
    _pm_verbatim_register_ids,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    out = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout


def _rules(verdict: dict, gated: list, pm: list | None) -> dict:
    args = f"{json.dumps(verdict)}, {json.dumps(gated)}" + (f", {json.dumps(pm)}" if pm is not None else "")
    return json.loads(_run_node(f"const log = () => {{}}; console.log(JSON.stringify({_JUDGE_VERDICT_RULES_JS}({args})));"))


def _not_met(register_rows: list) -> dict:
    return {
        "status": "not_met",
        "reason": "callers not switched",
        "differs_from_baseline": False,
        "observed": [{"kind": "command", "ref": "x", "result": "y"}],
        "clauses": [{"clause": "old op deleted", "status": "met", "observed_ref": "r"}],
        "falsifier": {"present": True, "ran": True, "verdict": "pass", "output_anchor": "ok"},
        "withheld_clauses": [{"clause": "callers switched", "rows": ["C12"], "register_rows": register_rows}],
    }


def test_a_withheld_clause_without_a_pm_row_still_reconciles_to_met():
    out = _rules(_not_met(["R2"]), ["C12"], ["R1"])
    assert out["status"] == "met" and out["reconciled_from"] == "not_met"


def test_a_withheld_clause_naming_a_pm_verbatim_row_stays_not_met():
    out = _rules(_not_met(["R1"]), ["C12"], ["R1"])
    assert out["status"] == "not_met" and "reconciled_from" not in out


def test_a_pm_verbatim_id_matching_a_plan_row_id_also_blocks():
    assert _rules(_not_met([]), ["C12"], ["C12"])["status"] == "not_met"


def test_no_pm_list_keeps_the_old_reconcile():
    assert _rules(_not_met(["R1"]), ["C12"], None)["status"] == "met"


def test_the_emitted_criterion_leg_passes_the_pm_ids_to_the_rules():
    expr = _never_stranding_criterion("x()", judge=True, withheld_ids=["C12"], pm_verbatim_ids=["R1"])
    assert '["C12"], ["R1"]' in expr


def test_the_pm_ids_alone_still_reach_the_rules():
    assert '[], ["R1"]' in _never_stranding_criterion("x()", judge=True, pm_verbatim_ids=["R1"])


def test_register_ids_with_pm_verbatims_origin_are_read_off_the_sizing(tmp_path: Path):
    rows = [
        {"id": "R1", "source_text": "t", "anchor": "pm_verbatims", "surface": "ui", "status": "open"},
        {"id": "R2", "source_text": "u", "anchor": "relayed", "surface": "ui", "status": "open"},
        {"id": "R3", "source_text": "v", "anchor": "p.3", "surface": "ui", "status": "open"},
    ]
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "s.yaml").write_text(
        "schema: sizing-object\n" + yaml.safe_dump({"requirement_register": {"rows": rows}}), encoding="utf-8"
    )
    plan = '---\nplan_id: "p1"\nsizing_object: "state/sizings/s.yaml"\n---\nbody\n'
    assert _pm_verbatim_register_ids(plan, tmp_path) == ["R1"]
    assert _pm_verbatim_register_ids("---\nplan_id: p1\n---\nbody\n", tmp_path) == []
