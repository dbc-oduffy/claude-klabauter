"""
coordinator_core.ops.dispatch_emit.tests.test_grind_ruling_loops_back_to_fix

Purpose: pins that a PM-bound hand-back raised inside the grind loop is adjudicated once per row, and an
actionable ruling re-enters fix in the same run (admission model and emitted JS twin), while a pm-only,
gated, unavailable or repeat hand-back stays handed back with its adjudication.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp

from .test_grind_compose import _compose

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"
_RULING = "apm (PM-delegated) apply the one-line allow-list entry"


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


_TRADEOFF_TRIAGE = {
    "row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S",
    "has_tradeoff": True, "tradeoff": "a corner", "ruling_on_tradeoff": "no-ruling",
}
_ACTIONABLE = {"verdict": "ruled", "pmOnly": False, "ruling": _RULING, "externalAction": "", "actionable": True}


def _run(adjudication, fixes, *, undo="undone"):
    budget = _Budget()
    fix_replies = list(fixes)

    def agent(kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(kind, 10))
        if kind == "triage":
            return [_TRADEOFF_TRIAGE]
        if kind == "adjudicate":
            return adjudication
        if kind == "fix":
            return fix_replies.pop(0)
        if kind == "verify":
            return {"outcome": "pass", "undo": {"outcome": undo}}
        if kind == "commit":
            return {"committed": True, "sha": "x", "rows": [{"row_id": "r0", "outcome": "settled", "sha": "x"}]}
        return {"outcome": "recorded", "swept": True}

    return gc.run_admission(
        [("b0", ["r0"])], _routing(), "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent,
    )


def test_actionable_ruling_re_enters_fix_and_commits_with_no_hand_back():
    result = _run(_ACTIONABLE, [{"outcome": "done"}])
    assert result["handed_back"] == []
    assert [s["outcome"] for s in result["settled"]] == ["committed"]
    assert result["spend"]["agent_calls_by_stage_kind"]["adjudicate"] == 1
    assert result["spend"]["agent_calls_by_stage_kind"]["fix"] == 1


def test_pm_only_verdict_stays_handed_back_once_with_its_adjudication():
    verdict = {**_ACTIONABLE, "verdict": "pm-only", "pmOnly": True, "actionable": False}
    result = _run(verdict, [])
    assert [h["type"] for h in result["handed_back"]] == ["needs-judgment"]
    assert result["handed_back"][0]["adjudication"]["pmOnly"] is True
    assert result["settled"] == []
    assert "fix" not in result["spend"]["agent_calls_by_stage_kind"]


def test_gated_external_action_stays_handed_back_even_when_marked_actionable():
    result = _run({**_ACTIONABLE, "externalAction": "merge to main"}, [])
    assert [h["type"] for h in result["handed_back"]] == ["needs-judgment"]
    assert result["handed_back"][0]["adjudication"]["actionable"] is False


def test_refix_that_hands_back_again_is_adjudicated_exactly_once():
    late_tradeoff = {
        "outcome": "done", "has_tradeoff": True, "tradeoff": "another corner", "ruling_on_tradeoff": "silent",
    }
    result = _run(_ACTIONABLE, [late_tradeoff])
    assert result["spend"]["agent_calls_by_stage_kind"]["adjudicate"] == 1
    assert [h["type"] for h in result["handed_back"]] == ["needs-judgment"]
    assert result["handed_back"][0]["adjudication"]["actionable"] is True
    assert result["settled"] == []


def test_row_with_writes_is_undone_before_the_re_fix():
    late_tradeoff = {
        "outcome": "done", "touched_files": ["a.py"], "has_tradeoff": True,
        "tradeoff": "a corner", "ruling_on_tradeoff": "no-ruling",
    }
    triage_clean = {**_TRADEOFF_TRIAGE, "has_tradeoff": False, "tradeoff": ""}
    budget = _Budget()
    fixes = [late_tradeoff, {"outcome": "done", "touched_files": ["a.py"]}]
    undo_sink: list = []

    def agent(kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(kind, 10))
        if kind == "triage":
            return [triage_clean]
        if kind == "adjudicate":
            return _ACTIONABLE
        if kind == "fix":
            return fixes.pop(0)
        if kind == "verify":
            return {"outcome": "pass", "undo": {"outcome": "undone"}}
        if kind == "commit":
            return {"committed": True, "sha": "x", "rows": [{"row_id": "r0", "outcome": "settled", "sha": "x"}]}
        return {"outcome": "recorded", "swept": True}

    result = gc.run_admission(
        [("b0", ["r0"])], _routing(), "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent, undo_paths_sink=undo_sink,
    )
    assert result["handed_back"] == []
    assert undo_sink and undo_sink[0]["paths"] == ["a.py"]
    assert result["spend"]["agent_calls_by_stage_kind"]["fix"] == 2
    assert [s["outcome"] for s in result["settled"]] == ["committed"]


def test_failed_undo_hands_back_stage_dead_and_does_not_loop():
    late_tradeoff = {
        "outcome": "done", "touched_files": ["a.py"], "has_tradeoff": True,
        "tradeoff": "a corner", "ruling_on_tradeoff": "no-ruling",
    }
    triage_clean = {**_TRADEOFF_TRIAGE, "has_tradeoff": False, "tradeoff": ""}
    budget = _Budget()

    def agent(kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(kind, 10))
        if kind == "triage":
            return [triage_clean]
        if kind == "adjudicate":
            return _ACTIONABLE
        if kind == "fix":
            return late_tradeoff
        if kind == "verify":
            return {"outcome": "pass", "undo": {"outcome": "undo-failed", "reason": "dirty"}}
        return {"outcome": "recorded", "swept": True}

    result = gc.run_admission(
        [("b0", ["r0"])], _routing(), "triage", reserve=gc.batch_reserve(1), budget=budget, agent=agent,
    )
    assert [h["type"] for h in result["handed_back"]] == ["stage-dead"]
    assert result["spend"]["agent_calls_by_stage_kind"]["fix"] == 1


def test_emitted_hand_back_adjudicates_before_the_undo_branch_and_outside_any_lock():
    script = _compose()
    body = script[script.index("async function _handBackRow("):]
    body = body[: body.index("async function _applyRouteRow(")]
    assert body.index("row.ruledOnce = true") < body.index("_UNDO_EXEMPT.includes(type)")
    ruled = body[body.index("row.ruledOnce = true"): body.index("_UNDO_EXEMPT.includes(type)")]
    assert "_adjudicateHandback(_entry)" in ruled
    assert "withLock" not in ruled
    assert "!row.ruledOnce" in body


def test_emitted_adjudication_helpers_are_visible_to_the_hand_back_path():
    script = _compose()
    outer_open = "try {\ntry { await runGrind(); }"
    assert script.index("async function _adjudicateHandback(") < script.index(outer_open)
    assert script.index("const PM_BOUND_TYPES") < script.index(outer_open)
    assert "if (route.kind === 'handback' && PM_BOUND_TYPES.includes(route.value))" in script
