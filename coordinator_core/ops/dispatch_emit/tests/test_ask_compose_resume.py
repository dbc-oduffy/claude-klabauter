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
    start = script.index("_gate.arm === 's') {\n    if (_resumeLedger)")
    s_arm = script[start:script.index("_gate.arm === 'm_plus'", start)]
    assert "if (_gate.resume_plan) { _planRel = _gate.resume_plan; } else {" in s_arm
    assert s_arm.index("_gate.resume_plan") < s_arm.index("phase('plan')") < s_arm.index("label: 'plan'")
    assert s_arm.count("agent(") == 1


def test_m_plus_baton_revises_the_resumed_plan():
    script = _compose()
    assert "planPath: _gate.resume_plan ?? _spineFallback ?? null" in script
    assert "planPath: null" not in script


def test_resume_rows_ride_the_gate_into_the_folded_stage_calls():
    script = _compose()
    assert "resume_rows: _reworded.resume_rows ?? null" in script
    assert "resume_rows: _scouted.resume_rows ?? null" in script
    spread = "...(_gate.resume_plan && _gate.resume_rows ? { only_rows: _gate.resume_rows } : {})"
    assert script.count(spread) >= 2
    rest = script.replace(spread, "").replace("resume_rows", "").replace("' with only_rows added,", "").replace("' with only_rows set to the ids of exactly the rows you added'", "")
    assert "only_rows" not in rest


def test_gate_reply_schema_carries_resume_plan():
    script = _compose()
    assert "resume_plan: { type: ['string', 'null'] }" in script or '"resume_plan"' in script
