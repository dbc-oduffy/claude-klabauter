"""
coordinator_core.ops.dispatch_emit.tests.test_grind_lessons_never_split

Purpose: a lessons-profile row whose triage names two or more items is routed by its verdict, never
split (split has no lessons schema, so the stage would die); other profiles still split. Pins the Python
admission composer and its emitted JS twin.
"""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import grind_compose as gc

from .test_grind_compose import _compose
from .test_grind_ruled_tradeoff_routing import _Budget, _routing

_ITEMS = ["first item", "second item"]


def _kinds_for(profile_name: str) -> list[str]:
    budget = _Budget()
    kinds: list[str] = []

    def agent(kind, unit_id):
        kinds.append(kind)
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(kind, 10))
        if kind == "triage":
            return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "items": _ITEMS}]
        if kind == "split":
            return {"outcome": "split", "children": ["c0", "c1"]}
        if kind == "fix":
            return {"outcome": "done"}
        if kind == "verify":
            return {"outcome": "pass"}
        if kind == "commit":
            return {"committed": True, "sha": "x"}
        return {"outcome": "recorded", "swept": True}

    gc.run_admission(
        [("b0", ["r0"])], _routing(), "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent, profile=profile_name,
    )
    return kinds


def test_lessons_row_with_two_items_is_never_split():
    kinds = _kinds_for("lessons")
    assert "triage" in kinds and "split" not in kinds


def test_bug_row_with_two_items_still_splits():
    assert "split" in _kinds_for("bug")


def _triage_ingest_line(script: str) -> str:
    stage = script[script.index("async function _triageBatch"):script.index("async function _closeBatch")]
    return next(ln for ln in stage.splitlines() if "_splitRow(recRow, row, _items)" in ln)


def test_emitted_js_twin_gates_the_split_on_the_never_split_profiles():
    script = _compose()
    assert 'const NEVER_SPLIT_PROFILES = ["lessons"];' in script
    guard = _triage_ingest_line(script)
    assert "!NEVER_SPLIT_PROFILES.includes(PROFILE_NAME)" in guard
    assert guard.index("_items.length >= 2") < guard.index("NEVER_SPLIT_PROFILES")


def test_never_split_set_names_exactly_lessons():
    assert gc.NEVER_SPLIT_PROFILES == frozenset({"lessons"})
