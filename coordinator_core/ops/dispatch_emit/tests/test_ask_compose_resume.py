"""compose_ask_script resume: a gate verdict's resume_plan is staged at S and revised at M+."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose():
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_s_arm_stages_resume_plan_and_authors_only_without_one():
    script = _compose()
    s_arm = script[script.index("_gate.arm === 's'"):script.index("_gate.arm === 'm_plus'")]
    assert "if (_gate.resume_plan) { _planRel = _gate.resume_plan; } else {" in s_arm
    assert s_arm.index("_gate.resume_plan") < s_arm.index("phase('plan')") < s_arm.index("label: 'plan'")
    assert s_arm.count("agent(") == 1


def test_m_plus_baton_revises_the_resumed_plan():
    script = _compose()
    assert "planPath: _gate.resume_plan ?? null" in script
    assert "planPath: null" not in script


def test_gate_reply_schema_carries_resume_plan():
    script = _compose()
    assert "resume_plan: { type: ['string', 'null'] }" in script or '"resume_plan"' in script
