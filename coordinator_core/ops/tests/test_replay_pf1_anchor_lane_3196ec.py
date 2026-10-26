"""P-F1 replay: 3196ec's plain-grep anchors (the `grep -c A f && grep -c B f` conjunctions with one
at-least-one expect, J2/J4) read as broke in-process though each stage held, so the gate pulled a plan the
EM had verified. The lane evaluates them at every size and a finding review already discharged is
never counted. Drives plan.readiness_verdict and anchor_verdicts, the existing entry points.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import _anchor_premise, anchor_lane
from coordinator_core.ops import plan_readiness_verdict as op
from coordinator_core.ops.dispatch_emit.run_truth_contract import (
    ANCHOR_BROKE,
    ANCHOR_HELD,
    ANCHOR_UNEVALUABLE,
    TSHIRTS,
)

REL = "docs/plans/2026-10-25-fixture.md"
MANIFEST = "coordinator_core/benchmarks/import-budget-manifest.json"
P5 = (
    "grep -c -F '\"baseline_module_count\": 18' " + MANIFEST
    + " && grep -c -F '\"headroom\": 8' " + MANIFEST
)
P11 = "grep -c claim_lookup_spike " + MANIFEST

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _anchors_yaml(anchors: list) -> str:
    if not anchors:
        return ""
    lines = ["anchor_premises:"]
    for aid, command, expect in anchors:
        lines += [
            f"  - id: {aid}",
            "    claim: the fixture fact holds",
            "    check:",
            f"      command: {json.dumps(command)}",
            f"      expect: {json.dumps(expect)}",
        ]
    return "\n".join(lines) + "\n"


def _plan(root: Path, anchors: list) -> Path:
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    manifest = root / MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{\n  "baseline_module_count": 18,\n  "headroom": 8\n}\n', encoding="utf-8")
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-25\n"
        "census:\n  - question: q\n    command: echo a\n    result: '1'\n"
        "capabilities: []\n"
        + _anchors_yaml(anchors)
        + "prime_exit_criterion:\n  statement: the op reports per class\n"
        "  derived_from: state/sizings/2026-10-15-fixture.yaml\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    return root / ".git"


def test_conjunction_with_one_expect_holds_through_the_readiness_entry_point(tmp_path):
    common = _plan(tmp_path, [("P5", P5, ">=1"), ("P11", P11, "0")])
    reply = op._handler({"plan": REL}, common)
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0, reply["reason"]
    by_id = {v["id"]: v for v in reply["anchor_verdicts"]}
    assert by_id["P5"]["verdict"] == "RESOLVES" and by_id["P5"]["source"] == "engine-check"
    assert by_id["P11"]["verdict"] == "RESOLVES" and by_id["P11"]["source"] == "engine-check"


def test_conjunction_where_one_stage_breaks_still_pulls(tmp_path):
    broken = P5.replace('"headroom": 8', '"headroom": 99')
    common = _plan(tmp_path, [("P5", broken, ">=1")])
    reply = op._handler({"plan": REL}, common)
    assert reply["verdict"] == "judgment" and reply["blocking_findings"] == 1
    assert reply["anchor_verdicts"][0]["verdict"] == "CONTRADICTED"


def test_anchor_verdicts_evaluates_with_no_rows_and_no_carry(tmp_path):
    _plan(tmp_path, [])
    anchors = [{"id": "P5", "claim": "c", "check": {"command": P5, "expect": ">=1"}}]
    (entry,) = _anchor_premise.anchor_verdicts(anchors, [], [], "", tmp_path)
    assert (entry["verdict"], entry["source"]) == ("RESOLVES", "engine-check")


def test_lane_outcomes_are_held_broke_unevaluable(tmp_path):
    _plan(tmp_path, [])
    held = anchor_lane.evaluate({"id": "a", "check": {"command": P5, "expect": ">=1"}}, tmp_path)
    broke = anchor_lane.evaluate({"id": "b", "check": {"command": P5, "expect": ">=2"}}, tmp_path)
    other = anchor_lane.evaluate({"id": "c", "check": {"command": "ls -la", "expect": "1"}}, tmp_path)
    assert (held.outcome, broke.outcome, other.outcome) == (ANCHOR_HELD, ANCHOR_BROKE, ANCHOR_UNEVALUABLE)
    assert other.observed


@pytest.mark.parametrize("size", TSHIRTS)
def test_lane_runs_at_every_size(size):
    assert anchor_lane.require_lane(size) == size


@pytest.mark.parametrize("size", [None, "standard", "XXXL"])
def test_lane_refuses_an_unknown_size(size):
    with pytest.raises(ValueError):
        anchor_lane.require_lane(size)


def _verified_ledger(root: Path) -> str:
    (root / "rev.md").write_text(
        "---\nagent_type: coordinator:code-reviewer\nfindings_ledger: {rows: 1}\n---\n# R\n\n"
        "## Findings Ledger\n\n```json\n"
        + json.dumps([{"id": "F1", "verified": True}])
        + "\n```\n",
        encoding="utf-8",
    )
    return str(root / "rev.md")


def test_a_finding_review_already_fixed_is_not_counted(tmp_path):
    common = _plan(tmp_path, [])
    undeclared = op._handler({"plan": REL}, common)
    assert undeclared["blocking_findings"] == 1
    assert any(b.startswith("anchor-premises-undeclared") for b in undeclared["blocking"])

    _plan(tmp_path, [("P5", P5, ">=1")])
    reviews = [{"reviewer": "patrik", "verdict": "WARN", "ledger": _verified_ledger(tmp_path)}]
    fixed = op._handler({"plan": REL, "reviews": reviews}, common)
    assert fixed["verdict"] == "ready" and fixed["blocking_findings"] == 0, fixed["reason"]
    assert fixed["blocking"] == []
