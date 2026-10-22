"""A keyless plan fails the bar, batches to the residue author, upgrades to `capabilities: []`, re-gates PASS.

One in-process walk over a single tmp_path plan: prep_gate, mise_residue and mise-prep-upgrade
agree on a missing `capabilities` key. The blitz_land leg is pinned in its own test file.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import yaml

from coordinator_core.roadmap import mise_residue as mr
from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate import _CLEAN_FM, _CLEAN_SPINE, _write_plan

_CLI_PATH = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "mise-prep-upgrade.py"


def _load_upgrade():
    name = "mise_prep_upgrade_key_agreement_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _residue(rows, root):
    return mr.residue_args(
        rows, None, repo_root=root, run_id="20261022T1700Z-abcd1234",
        gate_command="mise-prep-gate --repo-root repo", residue_agent_type=mr.RESIDUE_AGENT_TYPE,
        residue_model="sonnet", apm_model="opus",
        read_text=lambda r, plan: "",
    )


def test_keyless_plan_fails_batches_upgrades_and_regates(tmp_path):
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    fm = _CLEAN_FM.replace("capabilities: []\n", "")
    plan = _write_plan(tmp_path, frontmatter=fm, spine=_CLEAN_SPINE)

    report = pg.gate_plan(tmp_path, plan)
    assert report["verdict"] == pg.NOT_PREPPED
    cls = report["classes"]["CAPABILITIES"]
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == pg.CAPABILITIES_UNDECLARED_KIND == "capabilities-undeclared"

    row = {
        "plan": plan.name,
        "verdict": report["verdict"],
        "failing": ["CAPABILITIES/capabilities-undeclared"],
        "detail": [report["message"]],
        "outcome": "NOT-PREPPED",
        "seam": [{"class": "capabilities-undeclared", "blocking": True, "plan": plan.name}],
    }
    args = _residue([row], tmp_path)
    (batch,) = args["batches"]
    (member,) = batch["plans"]
    assert member["plan"] == plan.name
    assert member["seam"][0].startswith("seam: capabilities-undeclared")

    upgrade = _load_upgrade()
    assert upgrade.upgrade_plan(plan, check=False)["changed"] is True
    text = plan.read_text(encoding="utf-8")
    assert yaml.safe_load(text.split("---\n")[1])["capabilities"] == []

    regate = pg.gate_plan(tmp_path, plan)
    assert regate["classes"]["CAPABILITIES"]["status"] == "PASS"
