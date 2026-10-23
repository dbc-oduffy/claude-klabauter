"""An `execution-failed` seam drift earns one in-route repair round (`seam-fix:wave-N`) before the
plan halts: what the round commits, re-checks, refuses and bounds."""

from __future__ import annotations

import json

from .test_seam_leg_executes_declared_paths import _test_row_waves
from .test_seam_wave_boundary import CLEAN_REPLY, P1, P2, P3, _NODE, _compose, _refused, _row, _run_script

FALLBACK_PLAN = "state/mise-inventory/x.spine.md"


def _failed(*paths, extra=()):
    return {
        "verdict": "DRIFT",
        "per_plan": {},
        "findings": [{"class": "execution-failed", "path": p, "blocking": True} for p in paths] + list(extra),
    }


def _done(paths):
    return {"status": "DONE", "paths": paths}


def _count(out, prefix):
    return sum(c.startswith(prefix) for c in out["calls"])


def _tests_of(prompt):
    return json.loads(prompt.split("Tests: ", 1)[1].split(". Step 3", 1)[0])


def _rows_of(prompt):
    params = json.loads(prompt.split("Params, verbatim: `", 1)[1].split("`.", 1)[0])
    return [r["row"] for r in params["landed_rows"]]


def _run(tmp_path, replies, repair, waves=None):
    return _run_script(tmp_path, _compose(waves or _test_row_waves()), replies, repair=repair)


@_NODE
def test_a_landed_repair_commits_rechecks_and_dispatches_the_held_row(tmp_path):
    out = _run(tmp_path, [_failed("tests/test_a.py"), CLEAN_REPLY], _done(["a.py"]))
    assert _count(out, "seam-fix:wave-1") == 1
    assert _count(out, "commit:wave-1") == 2
    assert _count(out, "seam:wave-1") == 2
    assert out["haltedPlans"] == [] and out["drift"] == []
    assert "work:P2-C1" in out["calls"]
    first, second = [p for p in out["seamParams"] if "seam check for wave 1." in p]
    assert not any(r.startswith("seam-fix:") for r in _rows_of(second))
    assert _rows_of(second) == _rows_of(first)
    assert _tests_of(second) == _tests_of(first)


@_NODE
def test_a_flake_repair_with_no_paths_rechecks_without_a_second_commit(tmp_path):
    out = _run(tmp_path, [_failed("tests/test_a.py"), CLEAN_REPLY], _done([]))
    assert _count(out, "commit:wave-1") == 1
    assert _count(out, "seam:wave-1") == 2
    assert out["haltedPlans"] == []
    assert "work:P2-C1" in out["calls"]


@_NODE
def test_a_blocked_repair_halts_the_plan_with_its_reason(tmp_path):
    repair = {"status": "BLOCKED", "paths": [], "reason": "cannot reproduce the red"}
    out = _run(tmp_path, [_failed("tests/test_a.py"), CLEAN_REPLY], repair)
    assert _count(out, "seam:wave-1") == 1
    assert P1 in out["haltedPlans"]
    assert "work:P2-C1" not in out["calls"]
    reason = out["reasons"][P1]
    assert "in-route repair" in reason and "cannot reproduce the red" in reason


@_NODE
def test_a_repair_that_does_not_clear_the_drift_halts_after_exactly_one_round(tmp_path):
    out = _run(tmp_path, [_failed("tests/test_a.py")] * 3, _done(["a.py"]))
    assert _count(out, "seam-fix:wave-1") == 1
    assert _count(out, "seam:wave-1") == 2
    assert P1 in out["haltedPlans"] and "work:P2-C1" not in out["calls"]


@_NODE
def test_a_repair_outside_the_footprint_is_never_committed(tmp_path):
    out = _run(tmp_path, [_failed("tests/test_a.py"), CLEAN_REPLY], _done(["elsewhere.py"]))
    assert _count(out, "commit:wave-1") == 1
    assert _count(out, "seam:wave-1") == 1
    assert P1 in out["haltedPlans"]
    assert "outside the footprint" in out["reasons"][P1]


@_NODE
def test_an_unreachable_finding_beside_an_execution_failed_one_is_not_repairable(tmp_path):
    reply = _failed("tests/test_a.py", extra=[{"class": "unreachable", "path": "a.py", "blocking": True}])
    out = _run(tmp_path, [reply, CLEAN_REPLY], _done(["a.py"]))
    assert _count(out, "seam-fix:") == 0
    assert P1 in out["haltedPlans"]


@_NODE
def test_an_infra_refusal_is_never_routed_into_repair(tmp_path):
    refused = _refused("ETIMEDOUT reaching the engine")
    out = _run(tmp_path, [refused, refused], _done(["a.py"]))
    assert _count(out, "seam-fix:") == 0
    assert "engine defect" in out["reasons"][P2]


@_NODE
def test_a_two_plan_fix_attributes_to_the_committed_rows_plans_never_the_fallback(tmp_path):
    waves = [
        [_row("P1-C1", ["a.py", "tests/test_a.py"], P1), _row("P3-C1", ["z.py", "tests/test_z.py"], P3)],
        [_row("P2-C1", ["b.py"], P2, reads=["a.py"])],
    ]
    out = _run(tmp_path, [_failed("tests/test_a.py", "tests/test_z.py")] * 2, _done(["z.py"]), waves)
    assert _count(out, "seam-fix:wave-1") == 1
    assert _count(out, "commit:wave-1") == 2
    assert {P1, P3, P2} <= set(out["haltedPlans"])
    assert FALLBACK_PLAN not in out["haltedPlans"]
    assert sorted(d["path"] for d in out["drift"]) == ["tests/test_a.py", "tests/test_z.py"]
    assert {d["path"]: d["plans"] for d in out["drift"]} == {"tests/test_a.py": [P1], "tests/test_z.py": [P3]}
