"""
coordinator_core.ops.dispatch_emit.tests.test_grind_ruling_loops_back_to_fix

Purpose: pins that PM-bound hand-backs raised inside the grind loop are adjudicated by ONE post-loop call
(admission model and emitted JS twin): no mid-run ruling, no re-fix, every bound entry carries its
adjudication, and a missing, gated or pm-only verdict fails closed to pmOnly.
"""
from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp

from .test_grind_compose import _compose

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"
_RULING = "apply the one-line allow-list entry"


def _routing():
    profile = gp.load_profile("fixture", _FIXTURE_PROFILE_DIR)
    gp.validate_graph(profile)
    return gc.build_routing_table(profile)


class _Budget:
    total = None

    def __init__(self):
        self._spent = 0

    def spend(self, amount):
        self._spent += amount

    def spent(self):
        return self._spent

    def remaining(self):
        return None


def _triage(row):
    return {
        "row": row, "verdict": "confirmed-bug", "tshirt_size": "S",
        "has_tradeoff": True, "tradeoff": "a corner", "ruling_on_tradeoff": "no-ruling",
    }


def _ruled(row, **over):
    return {"row": row, "verdict": "ruled", "ruling": _RULING, "externalAction": "", "actionable": True, **over}


def _run(reply, rows=("r0",), *, call_sink=None):
    budget = _Budget()

    def agent(kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(kind, 10))
        if call_sink is not None:
            call_sink.append((kind, unit_id))
        if kind == "triage":
            return [_triage(r) for r in rows]
        if kind == "adjudicate":
            if isinstance(reply, Exception):
                raise reply
            return reply
        if kind == "commit":
            return {"committed": True, "sha": "x", "rows": []}
        return {"outcome": "recorded", "swept": True}

    return gc.run_admission(
        [("b0", list(rows))], _routing(), "triage",
        reserve=gc.batch_reserve(len(rows)), budget=budget, agent=agent,
    )


def test_actionable_ruling_does_not_re_enter_fix_and_rides_the_hand_back():
    result = _run({"rulings": [_ruled("r0")]})
    assert [h["type"] for h in result["handed_back"]] == ["needs-judgment"]
    adjudication = result["handed_back"][0]["adjudication"]
    assert adjudication["actionable"] is True and adjudication["ruling"] == _RULING
    assert result["settled"] == []
    assert "fix" not in result["spend"]["agent_calls_by_stage_kind"]


def test_several_hand_backs_make_exactly_one_adjudicate_call_after_the_loop():
    rows = ("r0", "r1", "r2")
    sink: list = []
    result = _run({"rulings": [_ruled(r) for r in rows]}, rows, call_sink=sink)
    kinds = [k for k, _ in sink]
    assert kinds.count("adjudicate") == 1
    assert result["spend"]["agent_calls_by_stage_kind"]["adjudicate"] == 1
    assert kinds.index("triage") < kinds.index("adjudicate") < kinds.index("ledger-sweep")
    assert all("adjudication" in h for h in result["handed_back"]) and len(result["handed_back"]) == 3


def test_a_row_missing_from_the_reply_fails_closed_to_pm_only():
    result = _run({"rulings": [_ruled("r0")]}, ("r0", "r1"))
    by_row = {h["row"]: h["adjudication"] for h in result["handed_back"]}
    assert by_row["r0"]["pmOnly"] is False
    assert by_row["r1"]["pmOnly"] is True and by_row["r1"]["verdict"] == "unavailable"


def test_pm_only_verdict_stays_handed_back_with_its_adjudication():
    result = _run({"rulings": [_ruled("r0", verdict="pm-only", actionable=False)]})
    assert result["handed_back"][0]["adjudication"]["pmOnly"] is True
    assert result["handed_back"][0]["adjudication"]["actionable"] is False


def test_spin_out_verdict_is_never_actionable():
    result = _run({"rulings": [_ruled("r0", verdict="spin-out")]})
    adjudication = result["handed_back"][0]["adjudication"]
    assert adjudication["verdict"] == "spin-out" and adjudication["pmOnly"] is False
    assert adjudication["actionable"] is False


def test_gated_external_action_stays_pm_only_even_when_marked_actionable():
    result = _run({"rulings": [_ruled("r0", externalAction="merge to main")]})
    assert result["handed_back"][0]["adjudication"]["pmOnly"] is True
    assert result["handed_back"][0]["adjudication"]["actionable"] is False


def test_a_throwing_adjudicator_fails_every_bound_hand_back_closed():
    result = _run(RuntimeError("boom"), ("r0", "r1"))
    assert [h["adjudication"]["pmOnly"] for h in result["handed_back"]] == [True, True]


def test_no_pm_bound_hand_back_spawns_no_adjudicate_call():
    budget = _Budget()
    kinds: list = []

    def agent(kind, unit_id):
        kinds.append(kind)
        if kind == "triage":
            return [{"row": "r0", "verdict": "duplicate", "tshirt_size": "S", "has_tradeoff": False, "tradeoff": ""}]
        return {"outcome": "recorded", "swept": True}

    gc.run_admission([("b0", ["r0"])], _routing(), "triage", reserve=gc.batch_reserve(1), budget=budget, agent=agent)
    assert "adjudicate" not in kinds


def test_emitted_hand_back_row_never_adjudicates_or_re_fixes():
    script = _compose()
    body = script[script.index("async function _handBackRow("):]
    body = body[: body.index("async function _applyRouteRow(")]
    for gone in ("_adjudicateHandback", "ruledOnce", "_reloop", "_ruledFixNode"):
        assert gone not in body and gone not in script.replace("async function _adjudicateHandback(", "")


def test_emitted_adjudicate_runs_once_after_the_loop_and_outside_it():
    script = _compose()
    outer_open = "try {\ntry { await runGrind(); }"
    assert script.index("async function _adjudicatePmBound(") < script.index(outer_open)
    assert script.index("const PM_BOUND_TYPES") < script.index(outer_open)
    assert script.count("await _adjudicatePmBound();") == 1
    assert script.index("await _adjudicatePmBound();") > script.index("await runGrind()")


def test_default_compose_has_one_sonnet_medium_adjudicate_call_and_no_apm():
    script = _compose()
    start = script.index("async function _adjudicateBatch(")
    block = script[start: script.index("async function _adjudicatePmBound(")]
    assert len(re.findall(r"label: 'adjudicate'", script)) == 1
    assert "coordinator:apm" not in script and "model: 'opus'" not in block
    assert "model: 'sonnet'" in block and "effort: 'medium'" in block
    assert block.count("await agent(") == 1
