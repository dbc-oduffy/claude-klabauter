"""Pure-read ratchet: the committed fast-tier ledger stays within the 15-minute fleet-floor budget
and lists every complete-marked test file as a move. Reads JSON and the AST only; no clock, no spawn.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LEDGER = _REPO / "state" / "test-tiers" / "fast-tier-ledger.json"
_spec = importlib.util.spec_from_file_location(
    "fast_tier_budget", _REPO / "coordinator" / "bin" / "fast-tier-budget.py"
)
budget = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(budget)


def _ledger(projected: float, moves: list[str]) -> dict:
    return {
        "budget_s": 900,
        "projected_floor_wall_s": projected,
        "moves": [{"id": m, "seconds": 1.0, "new_marker": "complete"} for m in moves],
    }


def test_projection_over_budget_fails_naming_the_budget():
    problems = budget.check_ledger(_ledger(901, []), [])
    assert len(problems) == 1 and "900s fast-tier budget" in problems[0]


def test_projection_at_budget_passes():
    assert budget.check_ledger(_ledger(900, []), []) == []


def test_complete_file_absent_from_moves_fails_naming_the_file():
    problems = budget.check_ledger(_ledger(100, ["a/test_x.py"]), ["a/test_x.py", "b/test_y.py"])
    assert len(problems) == 1 and "b/test_y.py" in problems[0]



def test_listed_move_without_the_mark_fails_naming_the_file():
    problems = budget.check_ledger(_ledger(100, ["a/test_x.py", "c/test_z.py"]), ["a/test_x.py"])
    assert len(problems) == 1 and "c/test_z.py" in problems[0]

def test_altered_budget_field_fails():
    ledger = _ledger(100, [])
    ledger["budget_s"] = 9000
    assert budget.check_ledger(ledger, [])


def _profiled(wall: float, cap: int, setup: float, tail: float, phase: str = "base") -> dict:
    return {
        "budget_s": 900,
        "projected_floor_wall_s": round(wall * cap / budget.FLOOR_CAP, 1),
        "measured_wall_s": wall,
        "box": {"cap": cap},
        "moves": [],
        "profile": {phase: {"session_setup_s": setup, "worker_tail_s": tail}},
    }


def test_split_projection_does_not_cap_scale_the_per_worker_constant():
    split = budget.split_projection(_profiled(wall=1000.0, cap=12, setup=90.0, tail=10.0))
    assert split == {"constant_overhead_s": 100.0, "projected_floor_wall_s": 1900.0}
    naive = 1000.0 * 12 / budget.FLOOR_CAP
    assert split["projected_floor_wall_s"] < naive


def test_split_projection_equals_naive_when_cap_is_floor_cap():
    split = budget.split_projection(_profiled(wall=700.0, cap=budget.FLOOR_CAP, setup=300.0, tail=5.0))
    assert split["projected_floor_wall_s"] == 700.0


def test_split_projection_absent_without_a_profile():
    assert budget.split_projection({"measured_wall_s": 10.0, "box": {"cap": 9}}) is None


def test_split_projection_prefers_the_after_profile():
    ledger = _profiled(wall=1000.0, cap=12, setup=90.0, tail=10.0)
    ledger["profile"]["after"] = {"session_setup_s": 40.0, "worker_tail_s": 0.0}
    assert budget.split_projection(ledger)["constant_overhead_s"] == 40.0


def test_check_ledger_gates_on_split_projection_when_a_profile_is_present():
    ledger = _profiled(wall=1000.0, cap=12, setup=90.0, tail=10.0)
    ledger["projected_floor_wall_s"] = 100.0
    ledger["split"] = budget.split_projection(ledger)
    problems = budget.check_ledger(ledger, [])
    assert len(problems) == 1 and "900s fast-tier budget" in problems[0]


def test_check_ledger_uses_naive_projection_without_split():
    ledger = _profiled(wall=1000.0, cap=12, setup=90.0, tail=10.0)
    assert len(budget.check_ledger(ledger, [])) == 1
    ledger["projected_floor_wall_s"] = 899.0
    assert budget.check_ledger(ledger, []) == []


def test_committed_ledger_is_within_budget_and_lists_every_complete_file():
    ledger = json.loads(_LEDGER.read_text(encoding="utf-8"))
    problems = budget.check_ledger(ledger, budget.scan_complete_files(_REPO))
    assert not problems, "\n".join(problems)
