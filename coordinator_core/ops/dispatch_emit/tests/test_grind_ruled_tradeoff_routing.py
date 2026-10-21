"""
coordinator_core.ops.dispatch_emit.tests.test_grind_ruled_tradeoff_routing

Purpose: pins the ruled-tradeoff routing of the queue-grind admission model and its emitted JS twin: a
row whose engine-read ``pm_ruling`` accepts its tradeoff routes to fix, a ruled row whose ruling does not
accept it is refused as ``repeat-refused`` naming the ruling, and an unruled tradeoff row still routes to
``needs-judgment``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit import pm_adjudication

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"
_GOLDEN = Path(__file__).parent / "fixtures" / "grind-fixture.golden.mjs"
_RULING = "apm (PM-delegated) accepted: the narrow-corner tradeoff is intended"


def _routing():
    profile = gp.load_profile("fixture", _FIXTURE_PROFILE_DIR)
    gp.validate_graph(profile)
    return gc.build_routing_table(profile)


class _Budget:
    def __init__(self):
        self._spent = 0
        self.total = None

    def spend(self, amount):
        self._spent += amount

    def spent(self):
        return self._spent

    def remaining(self):
        return None


def _run(triage_rec, fix_rec=None, *, pm_ruling=_RULING):
    budget = _Budget()
    fix_rec = fix_rec or {"outcome": "done"}

    def agent(kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(kind, 10))
        if kind == "triage":
            return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", **triage_rec}]
        if kind == "fix":
            return fix_rec
        if kind == "verify":
            return {"outcome": "pass"}
        if kind == "commit":
            return {"committed": True, "sha": "x"}
        return {"outcome": "recorded", "swept": True}

    return gc.run_admission(
        [("b0", ["r0"])], _routing(), "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent,
        pm_rulings={"r0": pm_ruling} if pm_ruling else None,
    )


def _types(result):
    return {h["type"] for h in result["handed_back"]}


def _tradeoff_triage(on_tradeoff):
    return {"has_tradeoff": True, "tradeoff": "a corner", "ruling_on_tradeoff": on_tradeoff}


@pytest.mark.parametrize(
    "ruling, on_tradeoff, expected",
    [
        ("", "accepts", ("handback", "needs-judgment")),
        ("", "no-ruling", ("handback", "needs-judgment")),
        (_RULING, "accepts", ("node", "fix")),
        (_RULING, "silent", ("handback", "needs-judgment")),
        (_RULING, "rejects", ("handback", "repeat-refused")),
        (_RULING, "no-ruling", ("handback", "repeat-refused")),
        (_RULING, "", ("handback", "repeat-refused")),
        (_RULING, "bogus", ("handback", "repeat-refused")),
    ],
)
def test_route_after_triage_four_way_rule(ruling, on_tradeoff, expected):
    assert gc.route_after_triage(_routing(), "triage", "confirmed-bug", "S", "a corner", ruling, on_tradeoff) == expected


def test_route_after_triage_without_tradeoff_ignores_the_ruling():
    assert gc.route_after_triage(_routing(), "triage", "confirmed-bug", "S", "", _RULING, "rejects") == ("node", "fix")


def test_ruled_and_accepted_tradeoff_at_triage_exit_reaches_fix():
    result = _run(_tradeoff_triage("accepts"))
    assert ("fix", "r0") in result["call_log"]
    assert "needs-judgment" not in _types(result) and "repeat-refused" not in _types(result)
    assert result["by_triage_check"]["ruled_tradeoff"] == 1


def test_ruled_and_accepted_tradeoff_at_fix_exit_proceeds_to_verify_without_a_hand_back():
    result = _run(
        {"has_tradeoff": False, "ruling_on_tradeoff": "no-ruling"},
        {"outcome": "done", "has_tradeoff": True, "tradeoff": "late", "ruling_on_tradeoff": "accepts",
         "close_result": {"old": "r0", "new": "archive/r0"}},
    )
    assert ("verify", "r0") in result["call_log"]
    assert "needs-judgment" not in _types(result) and "repeat-refused" not in _types(result)
    assert result["by_triage_check"]["ruled_tradeoff"] == 1
    assert result["by_triage_check"]["fix_late_tradeoff"] == 0


def test_repeat_hand_back_on_a_rejected_ruling_is_refused_naming_the_ruling():
    result = _run(_tradeoff_triage("rejects"))
    (hand,) = result["handed_back"]
    assert hand["type"] == "repeat-refused"
    assert _RULING in hand["reason"]
    assert hand["ruling"] == _RULING
    assert hand["tradeoff"] == "a corner"
    assert result["by_triage_check"]["repeat_refused"] == 1
    assert ("fix", "r0") not in result["call_log"]


def test_repeat_hand_back_at_fix_exit_is_refused_naming_the_ruling():
    result = _run(
        {"has_tradeoff": False, "ruling_on_tradeoff": "no-ruling"},
        {"outcome": "done", "has_tradeoff": True, "tradeoff": "late", "ruling_on_tradeoff": "rejects"},
    )
    (hand,) = result["handed_back"]
    assert hand["type"] == "repeat-refused" and hand["ruling"] == _RULING and _RULING in hand["reason"]
    assert result["by_triage_check"]["repeat_refused"] == 1


def test_unruled_tradeoff_row_still_routes_to_needs_judgment():
    result = _run(_tradeoff_triage("no-ruling"), pm_ruling="")
    (hand,) = result["handed_back"]
    assert hand["type"] == "needs-judgment" and "ruling" not in hand


def test_agent_claim_of_accepts_without_an_engine_read_ruling_stays_needs_judgment():
    result = _run(_tradeoff_triage("accepts"), pm_ruling="")
    (hand,) = result["handed_back"]
    assert hand["type"] == "needs-judgment"
    assert ("fix", "r0") not in result["call_log"]


def test_unruled_fix_exit_tradeoff_still_routes_to_needs_judgment():
    result = _run(
        {"has_tradeoff": False},
        {"outcome": "done", "has_tradeoff": True, "tradeoff": "late", "ruling_on_tradeoff": "accepts"},
        pm_ruling="",
    )
    assert _types(result) == {"needs-judgment"}
    assert result["by_triage_check"]["fix_late_tradeoff"] == 1


def test_silent_ruling_routes_to_needs_judgment():
    result = _run(_tradeoff_triage("silent"))
    assert _types(result) == {"needs-judgment"}


def test_cross_repo_hand_back_on_a_ruled_row_is_still_needs_judgment():
    result = _run(
        {"has_tradeoff": False},
        {"outcome": "done", "has_tradeoff": False, "touched_files": ["../other-repo/x.py"]},
    )
    (hand,) = result["handed_back"]
    assert hand["type"] == "needs-judgment" and "outside repo_root" in hand["reason"]


def test_emitted_script_carries_the_four_way_rule():
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert "function _tradeoffDisposition(tradeoff, pmRuling, onTradeoff)" in golden
    assert "function routeAfterTriage(verdict, size, tradeoff, pmRuling, onTradeoff)" in golden
    assert "routeAfterTriage(rec.verdict, rec.tshirt_size, tradeoff, row.pmRuling, rec.ruling_on_tradeoff)" in golden
    assert "_tradeoffDisposition(tradeoff, row.pmRuling, result.ruling_on_tradeoff)" in golden
    assert "pmRuling: _entry.pm_ruling || ''" in golden
    assert "ruled_tradeoff: 0, repeat_refused: 0" in golden
    assert gc._REPEAT_REFUSED_HEAD in golden


def test_repeat_refused_is_not_pm_bound():
    assert "repeat-refused" not in pm_adjudication.PM_BOUND_HANDBACK_TYPES
