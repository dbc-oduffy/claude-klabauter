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
        [node, "-e", script], capture_output=True, text=True, check=True,
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
def test_pass_and_not_present_are_unchanged(verdict):
    v = _met(falsifier={"present": True, "ran": True, "verdict": verdict})
    assert _rules(v) == v
