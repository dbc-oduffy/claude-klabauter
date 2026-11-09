"""An XS-arm sizing with a plan edge stages by plan_path in the gate agent's stage act; a direct sizing_path stage redirects to that plan."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

_STAGE_LEG = "label: 'xs-plan-stage'"
_EDGE_CLAUSE = ", except that with a resume_plan in the gate reply"


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
    script = _xs_script(monkeypatch).replace("\\'", "'")
    # The gate agent stages the run itself: a resume_plan in its reply swaps the sizing_path/writes params for plan_path.
    assert _STAGE_LEG not in script
    clause = script[script.index("except that with a resume_plan in the gate reply") :]
    clause = clause[: clause.index("Return its JSON reply verbatim as stage_reply")]
    assert "drop `writes`, `gated` and `sizing_path`" in clause
    assert "set `plan_path` to it and `commit_sizing_path` to the sizing_path value" in clause
    assert "add `only_rows` set to its resume_rows when present" in clause
def test_composed_xs_workflow_stages_a_resumed_plan_in_the_gate_agents_stage_act(monkeypatch):
    script = _xs_script(monkeypatch)
    assert _STAGE_LEG not in script
    gate_leg = next(ln for ln in script.splitlines() if "label: 'gate'" in ln)
    base, edge = gate_leg.split(_EDGE_CLAUSE, 1)
    assert "plan_path" not in base.split(ask_compose.OP_ASK_STAGE, 1)[1]
    assert "drop `writes`, `gated` and `sizing_path`" in edge
    assert "`plan_path` to it" in edge and "`only_rows` set to its" in edge
    assert "`commit_sizing_path` to the sizing_path value" in edge


def test_xs_adoption_and_plan_rel_cover_a_gate_reply_with_resume_plan(monkeypatch):
    script = _xs_script(monkeypatch)
    assert "its arm is XS, or is S with a resume_plan in the gate reply: run" in script.replace("\\'", "'")
    assert "if (!_gate.halt && (_gate.arm === 'xs' || " in script
    assert "{ await _adoptStageReply(_gate.stage_reply); }" in script
    assert "(_gate.arm === 'xs' || (_gate.arm === 's' && _gate.resume_plan)" in script
    assert "_gate.arm === 'xs' && _gate.resume_plan) { _planRel = _gate.resume_plan; }" in script
    assert "sizing_path: _sizingRel, gated: _gated" in script


def test_stop_after_script_composes_no_plan_stage_act(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    script = _compose(
        prompt=None, sizing_rel="state/sizings/x.yaml", stop_after=ask_compose.STOP_AFTER_PLAN,
    )
    assert _STAGE_LEG not in script and _EDGE_CLAUSE not in script


def test_direct_sizing_path_stage_of_a_plan_edge_xs_sizing_stages_the_plan(repo):  # noqa: F811
    _put(repo, tshirt="XS", route="dispatch", plan=PLAN_REL)
    _plan(repo)
    write_run_scripts(repo, "r1")
    m = stage(repo, run_id="r1", sizing_rel=REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["A1"]


def test_direct_sizing_path_stage_of_a_plan_edge_xs_sizing_with_a_missing_plan_is_refused(repo):  # noqa: F811
    _put(repo, tshirt="XS", route="dispatch", plan=PLAN_REL)
    with pytest.raises(AskStageError, match="^XS-MINT-MISROUTED: "):
        stage(repo, run_id="r1", sizing_rel=REL, writes=["pkg/a.py"])
