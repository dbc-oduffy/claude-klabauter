"""The judge verdict rules give a non-met with no `reason` its observation, and the refire reason carries it."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import _JUDGE_VERDICT_RULES_JS
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_commit_pending_e2e import (  # noqa: F401
    _item,
    _judge_result,
    _run,
    _xs_arm,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_FIXED = "judge returned not_met with no reason"


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


def test_a_not_met_with_no_reason_takes_its_observation():
    assert _rules({"status": "not_met", "observation": "X"})["reason"] == "X"


def test_an_empty_reason_takes_the_observation_capped_at_200():
    got = _rules({"status": "not_met", "reason": "", "observation": "y" * 300})["reason"]
    assert got == "y" * 200


def test_a_not_met_with_nothing_keeps_the_fixed_string():
    assert _rules({"status": "not_met"})["reason"] == _FIXED
    assert _rules({"status": "not_met", "observation": ""})["reason"] == _FIXED


def test_a_present_reason_is_untouched():
    assert _rules({"status": "not_met", "reason": "R", "observation": "X"})["reason"] == "R"


def test_a_met_with_nothing_observed_is_still_demoted():
    out = _rules({"status": "met", "observation": "X", "observed": []})
    assert out["status"] == "indeterminate" and out["reason"] == "met demoted: nothing observed"


def test_the_refire_reason_carries_the_judge_observation():
    judge = _judge_result(
        [_item("R1", "met")], reason=None, observation="X",
        falsifier={"verdict": "pass"}, clauses=[{"status": "not_met"}],
    )
    (out,) = _run([judge], judged=True)
    revise = out["revise_and_refire"]
    assert revise is not None and revise["reason"] == "criterion not_met: X", revise
