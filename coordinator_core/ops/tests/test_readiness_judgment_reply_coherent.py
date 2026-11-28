"""plan.readiness_verdict: a judgment-class review set always yields a reply that satisfies the coherence
clauses plan-blitz's honouredReadiness enforces, so a host echo of the op's reply is never dropped as incoherent.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import plan_readiness_verdict as op
from coordinator_core.ops.tests.test_plan_readiness_verdict import MECH, JUDG, PASS, REL, _gate, _plan

pytestmark = pytest.mark.complete

_VERDICTS = {"ready", "pulled", "judgment"}
_PULL_CLASSES = {"none", "mechanical", "substantive"}
_REVIEWS = [{"reviewer": "game-dev:staff-game-dev", "verdict": "APPROVED"}]
_INCOMPLETE_JUDGMENT = {"verdict": "INCOMPLETE", "mechanicalFindings": 0, "judgmentFindings": 5}
_INCOMPLETE_BOTH = {"verdict": "INCOMPLETE", "mechanicalFindings": 2, "judgmentFindings": 3}
_UNRESOLVED_ROW = {"verdict": "UNRESOLVED", "citation": "no/such/path.py", "questionClass": 1, "evidence": "absent"}
_RESOLVED_ROW = {"verdict": "RESOLVES", "citation": "coordinator_core/ops/plan_readiness_verdict.py", "questionClass": 1}


def assert_honourable(reply: dict) -> None:
    """The reply-side clauses of honouredReadiness (plan-blitz.mjs), applied to an op reply."""
    assert reply["verdict"] in _VERDICTS
    assert reply["pull_class"] in _PULL_CLASSES
    assert isinstance(reply["mechanical_repairs"], list)
    assert reply.get("prep_gate")
    assert (len(reply["mechanical_repairs"]) > 0) == (reply["pull_class"] == "mechanical")
    if reply["verdict"] == "ready":
        assert reply["mechanical_repairs"] == [] and reply["prep_gate"]["verdict"] == "PREPPED"


_CASES = {
    "incomplete-judgment-only": {"coverage": _INCOMPLETE_JUDGMENT, "reviews": _REVIEWS},
    "incomplete-judgment-with-resolved-premise": {
        "coverage": _INCOMPLETE_JUDGMENT, "reviews": _REVIEWS, "premise": {"rows": [_RESOLVED_ROW]},
    },
    "incomplete-judgment-with-unresolved-premise": {
        "coverage": _INCOMPLETE_JUDGMENT, "reviews": _REVIEWS, "premise": {"rows": [_RESOLVED_ROW, _UNRESOLVED_ROW]},
    },
    "incomplete-mechanical-and-judgment": {"coverage": _INCOMPLETE_BOTH, "reviews": _REVIEWS},
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_handler_reply_for_an_incomplete_coverage_review_set_is_honourable(tmp_path, name):
    common = _plan(tmp_path)
    reply = op._handler({"plan": REL, **_CASES[name]}, common)
    assert_honourable(reply)
    assert reply["verdict"] != "ready"


def test_judgment_findings_with_a_passing_gate_rule_judgment_none():
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), _INCOMPLETE_JUDGMENT, _REVIEWS)
    assert (out["verdict"], out["pull_class"], out["mechanical_repairs"]) == ("judgment", "none", [])


def test_mechanical_repairs_ride_only_a_mechanical_pull():
    mech = op.readiness_verdict(_gate(PASS, MECH), _INCOMPLETE_JUDGMENT, _REVIEWS)
    subst = op.readiness_verdict(_gate(MECH, JUDG), _INCOMPLETE_JUDGMENT, _REVIEWS)
    for out in (mech, subst):
        assert (len(out["mechanical_repairs"]) > 0) == (out["pull_class"] == "mechanical")
        assert out["verdict"] in _VERDICTS and out["pull_class"] in _PULL_CLASSES


def test_a_ready_verdict_only_ever_carries_a_prepped_gate(tmp_path):
    common = _plan(tmp_path)
    reply = op._handler({"plan": REL, "reviews": _REVIEWS}, common)
    assert_honourable(reply)
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), None, [])
    assert out["verdict"] == "ready" and out["mechanical_repairs"] == []
    unprepped = op.readiness_verdict(_gate(PASS, verdict="NOT-PREPPED"), None, [])
    assert (unprepped["verdict"], unprepped["pull_class"], unprepped["mechanical_repairs"]) == ("judgment", "none", [])
    assert "not PREPPED" in unprepped["reason"]
