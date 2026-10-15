"""plan.readiness_verdict: reply shape against the pinned schema, verdict classes, no skip param,
zero spawns and the process-time bar on a representative plan.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_readiness_verdict as op

REL = "docs/plans/2026-10-15-fixture.md"
SCHEMA = Path(op.__file__).resolve().parents[1] / "contract" / "readiness-verdict.schema.json"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _plan(root: Path, statement: str = "the op reports per class") -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-15\n"
        "census:\n  - question: q\n    command: grep -c a b\n    result: '1'\n"
        f"prime_exit_criterion:\n  statement: {statement}\n"
        "  derived_from: state/sizings/2026-10-15-fixture.yaml\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    return root / ".git"


def _validate(reply: dict) -> None:
    import jsonschema

    jsonschema.validate(reply, json.loads(SCHEMA.read_text(encoding="utf-8")))


def _gate(*classes: dict, verdict: str = "NOT-PREPPED") -> dict:
    return {
        "plan": REL,
        "verdict": verdict,
        "withheld_rows": [],
        "classes": {f"C{i}": c for i, c in enumerate(classes)},
        "message": "",
    }


PASS = {"status": "PASS", "kind": None, "mechanical": False, "repair": None}
MECH = {"status": "DEFECT", "kind": "prime-exit-suite-tier", "mechanical": True, "repair": "edit it"}
JUDG = {"status": "DEFECT", "kind": "census-incomplete", "mechanical": False, "repair": None}


def test_prepped_plan_without_findings_is_ready():
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), None, [])
    assert (out["verdict"], out["pull_class"], out["mechanical_repairs"]) == ("ready", "none", [])


def test_mechanical_prep_failure_pulls_with_repairs():
    out = op.readiness_verdict(_gate(PASS, MECH), None, [])
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    assert out["mechanical_repairs"] == [
        {"kind": "prime-exit-suite-tier", "target": "C1", "instruction": "edit it"}
    ]


def test_non_mechanical_prep_failure_is_substantive_and_drops_repairs():
    out = op.readiness_verdict(_gate(MECH, JUDG), None, [])
    assert (out["verdict"], out["pull_class"], out["mechanical_repairs"]) == ("pulled", "substantive", [])


def test_coverage_mechanical_findings_become_repairs():
    cov = {"verdict": "pull", "mechanicalFindings": ["add dlv id"], "judgmentFindings": []}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    assert "add dlv id" in out["mechanical_repairs"][0]["instruction"]


def test_coverage_counts_are_accepted():
    cov = {"verdict": "pull", "mechanicalFindings": 2, "judgmentFindings": 0}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert len(out["mechanical_repairs"]) == 2


def test_judgment_finding_needs_an_agent():
    cov = {"verdict": "pull", "mechanicalFindings": [], "judgmentFindings": ["scope unclear"]}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert (out["verdict"], out["pull_class"], out["mechanical_repairs"]) == ("judgment", "none", [])


def test_blocking_or_unknown_reviewer_verdict_fails_closed_to_judgment():
    for verdict in ("reject", "something-new"):
        out = op.readiness_verdict(
            _gate(PASS, verdict="PREPPED"), None, [{"reviewer": "patrik", "verdict": verdict}]
        )
        assert out["verdict"] == "judgment"


def test_approving_reviewers_keep_a_clean_plan_ready():
    out = op.readiness_verdict(
        _gate(PASS, verdict="PREPPED"),
        {"verdict": "approved", "mechanicalFindings": [], "judgmentFindings": []},
        [{"reviewer": "patrik", "verdict": "approved"}],
    )
    assert out["verdict"] == "ready"


def test_refused_prep_gate_is_substantive():
    out = op.readiness_verdict(_gate(PASS, verdict="REFUSED"), None, [])
    assert out["pull_class"] == "substantive"


@pytest.mark.parametrize("bad", [{"force": True}, {"skip": True}, {"skip_prep": True}])
def test_no_skip_or_force_param_is_accepted(tmp_path, bad):
    common = _plan(tmp_path)
    with pytest.raises(ValueError, match="unknown param"):
        op._handler({"plan": REL, **bad}, common)


def test_coverage_must_be_an_object(tmp_path):
    common = _plan(tmp_path)
    with pytest.raises(ValueError, match="coverage"):
        op._handler({"plan": REL, "coverage": "pull"}, common)


def test_handler_reply_validates_and_spawns_nothing_under_the_bar(tmp_path, monkeypatch):
    common = _plan(tmp_path)
    with _count_spawns_attributed(monkeypatch) as spawns:
        start = time.process_time()
        reply = op._handler({"plan": REL}, common)
        elapsed_ms = (time.process_time() - start) * 1000
    assert spawns == [], [s.argv for s in spawns]
    assert elapsed_ms < 500, elapsed_ms
    assert reply["plan"] == REL
    assert reply["prep_gate"]["plan"] == REL
    _validate(reply)


def test_handler_reply_for_a_suite_tier_statement_validates(tmp_path):
    common = _plan(tmp_path, statement="the fast tier is green")
    reply = op._handler({"plan": REL}, common)
    _validate(reply)
    assert reply["verdict"] in {"pulled", "judgment", "ready"}
