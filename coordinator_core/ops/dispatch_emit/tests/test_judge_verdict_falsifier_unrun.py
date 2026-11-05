"""A met whose recorded falsifier errored stays met when the judge observed the criterion."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import _JUDGE_VERDICT_RULES_JS

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PREFIX = "met; recorded falsifier did not run as authored: "


def _rules(verdict: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = f"const log = () => {{}}; console.log(JSON.stringify({_JUDGE_VERDICT_RULES_JS}({json.dumps(verdict)})));"
    out = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def _met(**over) -> dict:
    base = {
        "status": "met",
        "differs_from_baseline": True,
        "observed": [
            {"kind": "command", "ref": "x", "result": "y"},
            {"kind": "command", "ref": "x2", "result": "y2"},
        ],
        "clauses": [{"clause": "c", "status": "met", "observed_ref": "r"}],
        "falsifier": {"present": True, "ran": True, "verdict": "error", "output_anchor": "AttributeError"},
    }
    base.update(over)
    return base


def test_error_with_observation_stays_met_and_names_the_unrun_falsifier():
    out = _rules(_met())
    assert out["status"] == "met" and out["differs_from_baseline"] is True
    assert out["reason"] == _PREFIX + "AttributeError"


def test_the_unrun_reason_is_capped_at_200():
    f = {"present": True, "ran": True, "verdict": "error", "output_anchor": "z" * 500}
    out = _rules(_met(falsifier=f))
    assert out["status"] == "met" and len(out["reason"]) == 200 and out["reason"].startswith(_PREFIX)


def test_fail_still_demotes():
    f = {"present": True, "ran": True, "verdict": "fail", "output_anchor": "a"}
    out = _rules(_met(falsifier=f))
    assert out["status"] == "indeterminate" and out["reason"] == "met demoted: falsifier fail"
    assert out["differs_from_baseline"] is None


def test_error_without_difference_demotes():
    out = _rules(_met(differs_from_baseline=False))
    assert out["status"] == "indeterminate" and out["reason"] == "met demoted: falsifier error"


def test_error_with_nothing_observed_demotes():
    out = _rules(_met(observed=[]))
    assert out["status"] == "indeterminate" and "nothing observed" in out["reason"]


def test_error_with_a_non_met_clause_demotes():
    out = _rules(_met(clauses=[{"clause": "c", "status": "not_met", "observed_ref": "r"}]))
    assert out["status"] == "indeterminate" and "a clause is not met" in out["reason"]


@pytest.mark.parametrize("verdict", ["pass", "not_present"])
def test_pass_and_not_present_with_output_stay_met_and_carry_it(verdict):
    f = {"present": True, "ran": True, "verdict": verdict, "output_anchor": "3 passed"}
    out = _rules(_met(falsifier=f))
    assert out["status"] == "met" and out["falsifier"]["output"] == "3 passed"


@pytest.mark.parametrize("anchor", ["", "   ", None])
@pytest.mark.parametrize("verdict", ["pass", "not_present"])
def test_met_with_blank_falsifier_output_demotes(verdict, anchor):
    f = {"present": True, "ran": True, "verdict": verdict, "output_anchor": anchor}
    out = _rules(_met(falsifier=f))
    assert out["status"] == "indeterminate"
    assert out["reason"] == "met demoted: no captured falsifier output"
    assert out["differs_from_baseline"] is None


def test_met_without_a_falsifier_object_demotes():
    v = _met()
    del v["falsifier"]
    assert _rules(v)["status"] == "indeterminate"


def test_an_explicit_output_wins_over_the_anchor():
    f = {"present": True, "ran": True, "verdict": "pass", "output": "full run", "output_anchor": "a"}
    assert _rules(_met(falsifier=f))["falsifier"]["output"] == "full run"


def test_not_met_is_never_demoted_for_blank_output():
    out = _rules({"status": "not_met", "reason": "r", "falsifier": {"verdict": "pass", "output_anchor": ""}})
    assert out["status"] == "not_met"


def _stamped(call_expr: str, judge: bool) -> dict:
    from coordinator_core.ops.dispatch_emit.emit import _never_stranding_criterion

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = (
        "const log = () => {}; "
        f"({_never_stranding_criterion(call_expr, judge=judge)}).then(v => console.log(JSON.stringify(v)));"
    )
    out = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def test_the_engine_stamps_the_judge_agent_on_a_judge_result():
    from coordinator_core.ops.dispatch_emit.completion_gate import JUDGE_AGENT

    out = _stamped(f"Promise.resolve({json.dumps({**_met(falsifier={'verdict': 'pass', 'output_anchor': 'ok'}), 'authored_by': 'executor-template'})})", True)
    assert out["status"] == "met" and out["authored_by"] == JUDGE_AGENT


def test_the_indeterminate_fallback_carries_no_authorship():
    out = _stamped("Promise.reject(new Error('boom'))", True)
    assert out["status"] == "indeterminate" and "authored_by" not in out


def test_a_non_judge_leg_is_not_stamped():
    out = _stamped("Promise.resolve({status: 'met'})", False)
    assert "authored_by" not in out
