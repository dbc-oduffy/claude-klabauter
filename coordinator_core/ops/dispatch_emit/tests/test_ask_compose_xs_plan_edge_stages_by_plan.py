"""An XS-arm sizing with a plan edge stages by plan_path; the XS mint still refuses it by sizing_path."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

_STAGE_LEG = "label: 'xs-plan-stage'"
_PLAN_PARAMS = "plan_path: _gate.resume_plan"


def _xs_script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    return _compose(prompt=None, sizing_rel="state/sizings/x.yaml")


def test_xs_sizing_with_a_plan_edge_gates_with_resume_plan(repo):  # noqa: F811
    _put(repo, tshirt="XS", route="dispatch", plan=PLAN_REL)
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    v = gate(repo, REL, writes=["some/A2.py"])
    assert v.halt is None and v.arm == "xs"
    assert v.resume_plan == PLAN_REL and v.resume_rows == ["A2"]


def test_xs_sizing_without_a_plan_edge_has_no_resume_plan(repo):  # noqa: F811
    _put(repo, tshirt="XS", route="dispatch")
    v = gate(repo, REL, writes=["pkg/a.py"])
    assert v.halt is None and v.arm == "xs" and v.resume_plan is None


def test_composed_xs_workflow_stages_a_resumed_plan_by_plan_path(monkeypatch):
    script = _xs_script(monkeypatch)
    assert script.count(_STAGE_LEG) == 1
    leg = script.index(_STAGE_LEG)
    guard = script.rindex("\n  if (!_halted", 0, leg)
    call = script[guard:leg]
    assert "_gate.arm === 'xs' && _gate.resume_plan" in call and "!_resumeLedger" in call
    assert _PLAN_PARAMS in call and " sizing_path:" not in call


def test_xs_mint_stage_act_and_adoption_skip_a_gate_reply_with_resume_plan(monkeypatch):
    script = _xs_script(monkeypatch)
    assert "its arm is XS and it carries no resume_plan" in script.replace("\\'", "'")
    assert "(_gate.arm === 'xs' && !_gate.resume_plan)" in script
    assert "sizing_path: _sizingRel, gated: _gated" in script


def test_stop_after_script_composes_no_plan_stage_leg(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    script = _compose(
        prompt=None, sizing_rel="state/sizings/x.yaml", stop_after=ask_compose.STOP_AFTER_PLAN,
    )
    assert _STAGE_LEG not in script


def test_direct_sizing_path_stage_of_a_plan_edge_xs_sizing_is_still_refused(repo):  # noqa: F811
    _put(repo, tshirt="XS", route="dispatch", plan=PLAN_REL)
    _plan(repo)
    with pytest.raises(AskStageError, match="^XS-MINT-MISROUTED: "):
        stage(repo, run_id="r1", sizing_rel=REL, writes=["pkg/a.py"])
