"""S-arm plan stage at default composer args: plan.prep_gate loop with capped plan-author repair."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.ask_compose import (
    ARM_M_PLUS,
    ARM_S,
    OP_PREP_GATE,
    PREP_GATE_REPAIR_CAP,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _s_branch(script):
    """The S authored-plan branch: from the S-arm opener to the M+ arm opener."""
    start = script.rindex(f"_gate.arm === '{ARM_S}') {{", 0, script.index("label: 'plan'"))
    m_plus = script.index(f"_gate.arm === '{ARM_M_PLUS}') {{", start)
    return script[start:m_plus]


def test_gate_follows_plan_author_and_repair_carries_findings():
    s = _compose()
    branch = _s_branch(s)
    assert branch.index("label: 'plan'") < branch.index("label: 'plan-prep-gate'")
    assert OP_PREP_GATE in branch
    repair = next(ln for ln in branch.splitlines() if "label: 'plan-prep-repair'" in ln)
    assert "String(_pg.verdict)" in repair
    assert "String(_pg.message ?? '')" in repair


def test_repair_is_capped_and_halts_with_prep_gate_reason():
    branch = _s_branch(_compose())
    assert f"_pgRepairs >= {PREP_GATE_REPAIR_CAP}" in branch
    assert PREP_GATE_REPAIR_CAP == 3
    assert "reason: 'prep-gate: ' + _pgHalt" in branch


def test_fold_stage_adopts_once_after_the_gate_loop():
    branch = _s_branch(_compose())
    assert branch.count("_adoptStageReply(") == 1
    assert branch.index("_adoptStageReply(") > branch.rindex("plan-prep-repair")
    assert branch.index("_adoptStageReply(") > branch.index("label: 'plan-prep-gate'")
    assert "else { await _adoptStageReply(_pgReply); }" in branch


def test_stop_after_plan_gates_without_adopting():
    s = _compose(stop_after=STOP_AFTER_PLAN)
    assert "label: 'plan-prep-gate'" in s
    assert "_adoptStageReply(" not in s


def test_m_plus_arm_carries_no_plan_prep_gate():
    s = _compose()
    start = s.index(f"_gate.arm === '{ARM_M_PLUS}') {{")
    assert s.count("label: 'plan-prep-gate'") == 1
    assert s.index("label: 'plan-prep-gate'") < start
    m_plus_to_end = s[start:]
    assert not re.search(r"plan-prep-(gate|repair)", m_plus_to_end)


def test_refused_verdict_halts_before_any_repair():
    branch = _s_branch(_compose())
    refused = branch.index("_pg.verdict === 'REFUSED'")
    assert "_pgHalt = " in branch[refused : refused + 120]
    assert refused < branch.index(f"_pgRepairs >= {PREP_GATE_REPAIR_CAP}") < branch.index("_pgRepairs++")
