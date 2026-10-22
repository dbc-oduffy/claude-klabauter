"""A criterion unmet only on clauses a withheld row delivers lands with them carried open; any other unmet clause still halts."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import gated_rows
from coordinator_core.ops.dispatch_emit.emit import _JUDGE_VERDICT_RULES_JS
from coordinator_core.ops.dispatch_emit.register_gate import register_gate, withheld_register_rows
from coordinator_core.ops.review_mint.execute_review import (
    WITHHELD_CLAUSES_KEY,
    _widen_judge_schema,
    withheld_rows_clause,
)

_PLAN = "docs/plans/2026-10-22-w.md"
_SIZING = "state/sizings/2026-10-22-w.yaml"
_REGISTER = f"""status: routed
requirement_register:
  rows:
    - {{id: R1, source_text: t, anchor: relayed, surface: internal, status: not_met, claimed_by: [{_PLAN}], wired: false}}
"""
_PLAN_TEXT = f"""---
title: w
plan_id: plan-w
sizing_object: {_SIZING}
---
# W

## Tasks

```yaml plan-tasks
- id: C12
  title: switch callers
  change_kind: code-edit
  surface: a.py
  writes: [a.py]
  external_gate:
    - owner_repo: coordinator-content-repo
      requires: landed-work
      closure_key: {{kind: memo-thread, id: t}}
  register_rows: [R1]
  disposition: open
```
"""
_WITHHELD = [{"clause": "callers switched", "rows": ["C12"], "register_rows": ["R1"]}]


def _rules(verdict: dict, gated: list) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = (
        "const log = () => {}; console.log(JSON.stringify("
        f"{_JUDGE_VERDICT_RULES_JS}({json.dumps(verdict)}, {json.dumps(gated)})));"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def _not_met(**over) -> dict:
    base = {
        "status": "not_met",
        "reason": "callers not switched",
        "differs_from_baseline": False,
        "observed": [{"kind": "command", "ref": "x", "result": "y"}],
        "clauses": [{"clause": "old op deleted", "status": "met", "observed_ref": "r"}],
        WITHHELD_CLAUSES_KEY: _WITHHELD,
    }
    base.update(over)
    return base


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_only_withheld_clauses_unmet_lands_met_with_the_clauses_carried_open():
    out = _rules(_not_met(), ["C12", "C20"])
    assert out["status"] == "met"
    assert out[WITHHELD_CLAUSES_KEY] == _WITHHELD
    assert all(c["status"] == "met" for c in out["clauses"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_non_withheld_unmet_clause_still_halts():
    unmet = _not_met(clauses=[{"clause": "tests pass", "status": "not_met", "observed_ref": "r"}])
    assert _rules(unmet, ["C12"])["status"] == "not_met"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_withheld_clause_naming_a_row_that_is_not_withheld_still_halts():
    assert _rules(_not_met(), ["C99"])["status"] == "not_met"


def test_judge_prompt_clause_names_withheld_rows_and_register_rows():
    text = withheld_rows_clause([{"id": "C12", "title": "switch callers", "register_rows": ["R1"]}])
    assert "C12 (switch callers) [register R1]" in text and WITHHELD_CLAUSES_KEY in text
    assert withheld_rows_clause([]) == ""


def test_judge_schema_accepts_withheld_clauses():
    props = json.loads(_widen_judge_schema(json.dumps({"type": "object", "properties": {}})))["properties"]
    assert WITHHELD_CLAUSES_KEY in props


def test_gated_row_carries_its_title_and_register_rows():
    raw = {"C12": {"id": "C12", "title": "switch callers", "register_rows": ["R1"],
                   "external_gate": [{"owner_repo": "coordinator-content-repo", "requires": "landed-work"}]}}
    (g,) = gated_rows([{"id": "C12", "reason": "external_gate"}], raw)
    assert g.title == "switch callers" and g.register_rows == ("R1",)
    assert g.to_json()["register_rows"] == ["R1"]


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _SIZING).write_text(_REGISTER, encoding="utf-8")
    (tmp_path / _PLAN).write_text(_PLAN_TEXT, encoding="utf-8")
    return tmp_path


def _review(withheld=True):
    judged = {"id": "R1", "claim": "this-plan", "status": "not_met", "wired": False}
    crit = {"status": "met", "observation": "ok", "register_rows": [judged]}
    if withheld:
        crit[WITHHELD_CLAUSES_KEY] = _WITHHELD
    return {"criterion": crit}


def test_register_gate_leaves_a_withheld_row_open_without_demoting(tmp_path):
    root = _repo(tmp_path)
    review = _review()
    assert withheld_register_rows(root, _PLAN, review["criterion"]) == frozenset({"R1"})
    assert register_gate(root, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


def test_register_gate_still_demotes_an_unmet_row_no_withheld_clause_names(tmp_path):
    root = _repo(tmp_path)
    review = _review(withheld=False)
    reason = register_gate(root, _PLAN, review)
    assert reason is not None and "R1" in reason
    assert review["criterion"]["status"] == "not_met"


def test_register_gate_ignores_a_withheld_clause_the_plan_does_not_confirm(tmp_path):
    root = _repo(tmp_path)
    (root / _PLAN).write_text(_PLAN_TEXT.replace("disposition: open", "disposition: coded"), encoding="utf-8")
    review = _review()
    assert register_gate(root, _PLAN, review) is not None
    assert review["criterion"]["status"] == "not_met"


_SLOT_PLAN = _PLAN_TEXT.replace(
    """  external_gate:
    - owner_repo: coordinator-content-repo
      requires: landed-work
      closure_key: {kind: memo-thread, id: t}
""",
    "  needs_slot: true\n",
)


def test_needs_slot_row_is_withheld_for_the_judge_without_widening_gated():
    from coordinator_core.ops.dispatch_emit.emit import _withheld_judge_rows

    (row,) = _withheld_judge_rows(_SLOT_PLAN.replace("\r\n", "\n"), [])
    assert row == {"id": "C12", "title": "switch callers", "register_rows": ["R1"]}
    done = _SLOT_PLAN.replace("disposition: open", "disposition: coded")
    assert _withheld_judge_rows(done, []) == []


def test_register_gate_leaves_a_needs_slot_row_open(tmp_path):
    root = _repo(tmp_path)
    (root / _PLAN).write_text(_SLOT_PLAN, encoding="utf-8")
    review = _review()
    assert register_gate(root, _PLAN, review) is None
    assert review["criterion"]["status"] == "met"


def test_stage_manifest_carries_slot_withheld_apart_from_gated():
    from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest

    base = {"run_dir": "r", "rows": [], "review_declared_paths": [], "marker_path": "m"}
    slot = [{"id": "C12", "title": "t", "register_rows": ["R1"]}]
    m = StageManifest.from_json({**base, "slot_withheld": slot})
    assert m.slot_withheld[0]["id"] == "C12" and m.gated == ()
    assert m.to_json()["slot_withheld"] == slot
    assert "slot_withheld" not in StageManifest.from_json(base).to_json()
