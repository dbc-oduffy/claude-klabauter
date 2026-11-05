"""S-arm plan stage at default composer args: plan.prep_gate runs as the tail of the plan author and of each capped repair."""

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


def _m_plus_open(script, start=0):
    """Index of the M+ arm opener, with or without the roadmap arm folded into it."""
    m = re.compile(rf"_gate\.arm === '{ARM_M_PLUS}'(?: \|\| _gate\.arm === 'roadmap'\))?\) \{{").search(script, start)
    assert m, "no M+ arm opener"
    return m.start()


def _s_branch(script):
    """The S authored-plan branch: from the S-arm opener to the M+ arm opener."""
    start = script.rindex(f"_gate.arm === '{ARM_S}') {{", 0, script.index("label: 'plan'"))
    m_plus = _m_plus_open(script, start)
    return script[start:m_plus]


def _leg(branch, label):
    return next(ln for ln in branch.splitlines() if f"label: '{label}'" in ln)


def test_no_gate_leg_runs_and_the_author_and_repair_tails_run_the_gate_op():
    s = _compose()
    assert "label: 'plan-prep-gate'" not in s
    branch = _s_branch(s)
    # The plan author runs the gate as its last act and returns the verdict; no separate gate agent runs.
    assert "label: 'plan-prep-gate'" not in s
    assert branch.index(OP_PREP_GATE) < branch.index("label: 'plan'") < branch.index("_pgSrc.prep_verdict")
    repair = next(ln for ln in branch.splitlines() if "label: 'plan-prep-repair'" in ln)
    assert "String(_pg.verdict)" in repair
    assert "String(_pg.message ?? '')" in repair
    assert repair.index("plan_rel") < repair.index(OP_PREP_GATE)


def test_the_loop_reads_the_authors_verdict_first_then_each_repairs():
    branch = _s_branch(_compose())
    assert "_pgSrc = _authored" in branch
    assert branch.index("_pgSrc = _authored") < branch.index("_pgSrc = _pgRep")
    assert "_pgSrc.prep_verdict" in branch and "_pgSrc.prep_message" in branch


def test_repair_is_capped_and_halts_with_prep_gate_reason():
    branch = _s_branch(_compose())
    assert f"_pgRepairs >= {PREP_GATE_REPAIR_CAP}" in branch
    assert PREP_GATE_REPAIR_CAP == 3
    assert "reason: 'prep-gate: ' + _pgHalt" in branch


def test_fold_stage_adopts_once_after_the_gate_loop():
    branch = _s_branch(_compose())
    assert branch.count("_adoptStageReply(") == 1
    assert branch.index("_adoptStageReply(") > branch.rindex("plan-prep-repair")
    assert branch.index("_adoptStageReply(") > branch.index("_pgSrc.prep_verdict")
    assert "else { await _adoptStageReply(_pgReply); }" in branch


def test_stop_after_plan_gates_without_adopting():
    s = _compose(stop_after=STOP_AFTER_PLAN)
    assert OP_PREP_GATE in s and "_pgSrc.prep_verdict" in s
    assert "_adoptStageReply(" not in s


def test_m_plus_arm_carries_no_plan_prep_gate():
    s = _compose()
    start = _m_plus_open(s)
    # The plan author's prompt and the repair prompt are the only emissions, both in the S branch.
    assert s.count(OP_PREP_GATE) == 2
    assert s.rindex(OP_PREP_GATE) < start
    m_plus_to_end = s[start:]
    assert OP_PREP_GATE not in m_plus_to_end
    assert not re.search(r"plan-prep-(gate|repair)", m_plus_to_end)
    assert OP_PREP_GATE not in m_plus_to_end


def test_refused_verdict_halts_before_any_repair():
    branch = _s_branch(_compose())
    refused = branch.index("_pg.verdict === 'REFUSED'")
    assert "_pgHalt = " in branch[refused : refused + 120]
    assert refused < branch.index(f"_pgRepairs >= {PREP_GATE_REPAIR_CAP}") < branch.index("_pgRepairs++")


def test_a_reply_with_no_verdict_logs_and_leaves_the_loop():
    branch = _s_branch(_compose())
    assert "incident: plan prep gate returned no parseable reply" in branch
