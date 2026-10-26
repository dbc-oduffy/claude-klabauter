"""land_run's terminal objective gate: done only on a passing check, else the loop's recorded exit."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import objective_contract as oc
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_KEYS, read_run_outcome

RUN = "run-1"


def _run_dir(root: Path, *, loop=None, objective=True) -> Path:
    d = root / "state" / "warp" / RUN
    d.mkdir(parents=True)
    if objective:
        (d / oc.OBJECTIVE_FILE).write_text(
            json.dumps({"verbatim": "v", "falsifier_script": "f.py", "axes": [{"id": "a"}, {"id": "b"}]}),
            encoding="utf-8",
        )
    if loop is not None:
        (d / oc.LOOP_FILE).write_text("".join(json.dumps(x) + "\n" for x in loop), encoding="utf-8")
    return d


def _check(*passes, kind="check", **kw):
    return {"kind": kind, "wave": 1, "iteration": 0, "spend_usd": 1.5,
            "axes": [{"axis": a, "pass": p} for a, p in zip("ab", passes)], **kw}


def _review(status="met", clauses=None):
    return {"criterion": {"status": status, "clauses": clauses if clauses is not None else [{"status": "met"}],
                          "observed": ["x"]}}


def test_pass_closes_done_and_settles_met(tmp_path):
    _run_dir(tmp_path, loop=[_check(True, True)])
    review = _review()
    gate = land_run._objective_gate(tmp_path, RUN, review)
    assert gate["status"] == "met"
    assert gate["outcome"] == oc.OUTCOME_MET
    assert gate["unmet_axes"] == []
    assert review["criterion"]["status"] == "met"


def test_fail_with_impossible_rethink_closes_impossible_naming_axes(tmp_path):
    _run_dir(tmp_path, loop=[
        _check(True, False),
        {"kind": oc.LINE_RETHINK, "wave": 2, "iteration": 1, "trigger": "n", "verdict": "impossible"},
    ])
    review = _review()
    gate = land_run._objective_gate(tmp_path, RUN, review)
    assert (gate["status"], gate["outcome"], gate["exit_reason"]) == ("not_met", "impossible", "impossible")
    assert gate["unmet_axes"] == ["b"]
    assert review["criterion"]["status"] == "not_met"
    assert review["criterion"]["unmet_axes"] == ["b"]
    assert gate["summary"] == {"checks": 1, "reacher_iterations": 0, "spend_usd": 1.5}


def test_ceiling_rethink_closes_doctrine_conflict(tmp_path):
    _run_dir(tmp_path, loop=[_check(False, False), {"kind": oc.LINE_RETHINK, "verdict": "ceiling"}])
    gate = land_run._objective_gate(tmp_path, RUN, _review())
    assert (gate["outcome"], gate["exit_reason"]) == ("doctrine_conflict", "ceiling")
    assert oc.is_terminal(gate["outcome"])
    assert gate["unmet_axes"] == ["a", "b"]


def test_fail_without_recorded_exit_is_never_done_or_partial(tmp_path):
    _run_dir(tmp_path, loop=[_check(True, False)])
    gate = land_run._objective_gate(tmp_path, RUN, _review())
    assert gate["status"] == "not_met"
    assert gate["outcome"] == oc.OUTCOME_EXHAUSTED
    assert not oc.is_terminal(gate["outcome"])
    assert "partial" not in json.dumps(gate["objective_loop"])


def test_empty_loop_closes_doctrine_conflict_no_check(tmp_path):
    _run_dir(tmp_path)
    review = _review()
    gate = land_run._objective_gate(tmp_path, RUN, review)
    assert (gate["status"], gate["outcome"], gate["exit_reason"]) == ("not_met", "doctrine_conflict", "no-check")
    assert gate["unmet_axes"] == ["a", "b"]
    assert review["criterion"]["status"] == "not_met"


def test_indeterminate_derivation_settles_to_met_or_not_met(tmp_path):
    _run_dir(tmp_path, loop=[_check(True, True)])
    review = _review(clauses=[])
    land_run._derive_judge_status(review)
    assert review["criterion"]["status"] == "indeterminate"
    land_run._objective_gate(tmp_path, RUN, review)
    assert review["criterion"]["status"] == "met"

    other = tmp_path / "other"
    other.mkdir()
    _run_dir(other, loop=[_check(False, False)])
    review = _review(clauses=[{"status": "met"}, {"status": "indeterminate"}])
    land_run._derive_judge_status(review)
    assert review["criterion"]["status"] == "indeterminate"
    land_run._objective_gate(other, RUN, review)
    assert review["criterion"]["status"] == "not_met"


def test_run_without_objective_is_unchanged(tmp_path):
    _run_dir(tmp_path, objective=False)
    review = _review(clauses=[])
    land_run._derive_judge_status(review)
    before = json.dumps(review, sort_keys=True)
    assert land_run._objective_gate(tmp_path, RUN, review) is None
    assert json.dumps(review, sort_keys=True) == before
    assert land_run._objective_gate(tmp_path, "missing-run", review) is None


def test_objective_loop_lands_in_run_outcome(tmp_path):
    assert "objective_loop" in RUN_OUTCOME_KEYS
    _run_dir(tmp_path, loop=[_check(True, True)])
    review = _review()
    gate = land_run._objective_gate(tmp_path, RUN, review)
    land_run._record_objective_outcome(tmp_path, RUN, review, gate)
    data = json.loads((tmp_path / "state" / "warp" / RUN / "run-outcome.json").read_text(encoding="utf-8"))
    assert data["objective_loop"]["outcome"] == oc.OUTCOME_MET
    assert read_run_outcome(tmp_path, RUN)["objective_loop"]["unmet_axes"] == []


def test_workflow_recorded_impossible_exit_survives_the_landing_fold(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import write_run_outcome

    _run_dir(tmp_path, loop=[_check(True, False)])
    recorded = {"outcome": "impossible", "exit_reason": "impossible", "rethinks": [{"trigger": "attempts", "verdict": "impossible"}]}
    write_run_outcome(tmp_path, RUN, {"inline_review": _review(), "objective_loop": recorded})
    review = _review()
    gate = land_run._objective_gate(tmp_path, RUN, review)
    assert (gate["outcome"], gate["exit_reason"]) == ("impossible", "impossible")
    land_run._record_objective_outcome(tmp_path, RUN, review, gate)
    assert read_run_outcome(tmp_path, RUN)["objective_loop"]["rethinks"][0]["trigger"] == "attempts"


def test_passing_falsifier_never_softens_a_judged_not_met(tmp_path):
    _run_dir(tmp_path, loop=[_check(True, True)])
    review = _review(status="not_met")
    gate = land_run._objective_gate(tmp_path, RUN, review)
    assert gate["status"] == "met"
    assert review["criterion"]["status"] == "not_met"


def test_unreadable_objective_is_never_done(tmp_path):
    d = _run_dir(tmp_path, loop=[_check(True, True)])
    (d / oc.OBJECTIVE_FILE).write_text("{not json", encoding="utf-8")
    review = _review()
    gate = land_run._objective_gate(tmp_path, RUN, review)
    assert gate["status"] == "not_met"
    assert review["criterion"]["status"] == "not_met"
