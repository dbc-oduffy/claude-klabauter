"""compose_ask_script: a gate-minted spine takes one plan-review agent and reaches plan-blitz only on failure."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_compose import _GATE_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _m_branch(script):
    start = script.index("_gate.arm === 'm_plus') {")
    return script[start : script.index("\n  }\n", start)]


def test_gate_schema_carries_spine_plan():
    assert "spine_plan" in _GATE_SCHEMA["properties"]


def test_plan_review_agent_is_guarded_by_spine_plan():
    branch = _m_branch(_compose())
    assert "if (_gate.spine_plan && !_gate.resume_plan) {" in branch
    review = branch.index("label: 'plan-review'")
    assert branch.index("_gate.spine_plan && !_gate.resume_plan") < review
    assert "agentType: 'coordinator:staff-eng'" in branch
    assert "phase: 'plan'" in branch[review - 400 : review + 400]
    assert "plan.readiness_verdict" in branch


def test_blitz_is_reached_only_when_the_review_left_no_plan():
    branch = _m_branch(_compose())
    assert branch.index("if (_spineReview?.verdict === 'ready')") < branch.index("if (!_planRel && !_halted) {")
    assert branch.index("if (!_planRel && !_halted) {") < branch.index("await planBlitz(")
    assert "_gate.resume_plan ?? _spineFallback ?? null" in branch


def test_not_ready_review_halts_with_its_verdict_and_skips_blitz():
    branch = _m_branch(_compose())
    assert "reason: 'spine plan review verdict '" in branch
    assert branch.index("if (_spineReview?.verdict === 'ready')") < branch.index("spine plan review verdict")
    assert "if (!_planRel && !_halted) {" in branch
    assert "review: _spineReview" in branch


def test_s_and_xs_arms_carry_no_spine_text():
    script = _compose()
    m_start = script.index("_gate.arm === 'm_plus') {")
    assert "spine_plan" not in script[:m_start].split("_gate.arm === 's')", 1)[-1].split("_gate.arm === 'm_plus'")[0]
    assert "spine-direct" not in script
