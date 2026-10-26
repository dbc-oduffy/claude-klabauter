"""The roster's exit-criterion judge rides inside the one warp-ask Verify agent on every arm."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.emission_receipt import _load_review_inputs
from coordinator_core.ops.dispatch_emit.run_truth_contract import ARM_ROADMAP, ARMS
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _compose
from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN


#: The roadmap arm composes no Verify leg (it halts at a roadmap), so it rosters no judge.
VERIFY_ARMS = tuple(a for a in ARMS if a != ARM_ROADMAP)


def _real_roster_kw() -> dict:
    fragment, schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    return {"review_roster_fragment": fragment, "review_stage_schemas": schemas}


def _verify_leg(script: str) -> str:
    assert script.count("label: 'verify'") == 1
    return script[script.index("label: 'verify'") :]


def _assert_judge_in_verify(script: str) -> None:
    _verify_leg(script)
    assert "(3) VERDICT." in script
    assert "`judge`" in script
    assert "_falsifierResult = (_verify && _verify.judge) ? " in script
    assert "unjudged: verify leg returned no verdict" in script
    assert "_reconcileJudged();" in script


@pytest.mark.parametrize("arm", VERIFY_ARMS)
def test_a_judge_rostered_run_judges_inside_the_verify_agent_on_every_arm(monkeypatch, arm):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", **_JUDGE_KW)
    _assert_judge_in_verify(script)
    assert "agentType: 'coordinator:criterion-judge'" not in script


@pytest.mark.parametrize("arm", VERIFY_ARMS)
def test_the_unjudged_side_stays_unjudged_on_every_arm(monkeypatch, arm):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml")
    assert "_falsifierResult" not in script
    assert "_reconcileJudged();" not in script


@pytest.mark.parametrize("arm", VERIFY_ARMS)
def test_the_shipped_roster_judge_rides_the_verify_agent_on_every_arm(monkeypatch, arm):
    try:
        kw = _real_roster_kw()
    except Exception as exc:  # DoE roster not installed on this box
        pytest.skip(f"shipped roster unloadable: {exc}")
    judge_stages = [s for s in kw["review_roster_fragment"]["execute_review"]["stages"] if s.get("kind") == "judge"]
    assert len(judge_stages) == 1 and judge_stages[0]["agents"], "shipped roster declares no judge"
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", **kw)
    _assert_judge_in_verify(script)
