"""A plan moved into its month folder mid-blitz: every later leg is handed the moved path, not the authored one."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import (
    ARM_M_PLUS,
    ARM_S,
    OP_PREP_GATE,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.conftest import compose_ask_script

pytestmark = pytest.mark.usefixtures("m_plus_known_arm")

FLAT = "docs/plans/2026-11-11-hnd-thing-cc4d13.md"
MONTH = "docs/plans/2026-11/2026-11-11-hnd-thing-cc4d13.md"
_MOVED = "A plan moved to docs/plans/<YYYY-MM>/ is the same plan"


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _m_plus_branch(script):
    start = script.index(f"_gate.arm === '{ARM_M_PLUS}') {{")
    return script[start : script.index("\n  }\n", start)]


def _follower_js(script):
    """The composed plan-path follower, lifted so its behaviour is read off the emitted text."""
    m = re.search(r"const _planMoves = \{\}; .*?const _planNow = [^\n]*", script)
    assert m, "no plan-path follower in the composed script"
    return m.group(0)


def test_the_follower_keys_by_file_name_so_a_month_move_is_the_same_plan():
    js = _follower_js(_compose())
    assert r"split(/[\\/]/).pop()" in js
    assert js.index("_planSeen") < js.index("_planNow")
    assert "(_planMoves[_planKey(p)] = p)" in js
    assert "_planMoves[_planKey(p)] || p" in js


def test_the_spine_review_hands_its_moved_path_to_the_blitz():
    branch = _m_plus_branch(_compose())
    assert "_spineFallback = _planSeen(_spineReview?.plan_rel) || _planNow(_gate.spine_plan);" in branch
    assert branch.index("_spineFallback = _planSeen(") < branch.index("planPath: _gate.resume_plan ?? _spineFallback ?? null")


def test_the_review_leg_reads_the_followed_spine_path_and_is_told_a_move_is_the_same_plan():
    branch = _m_plus_branch(_compose())
    review = next(ln for ln in branch.splitlines() if "label: 'plan-review'" in ln)
    assert "_planNow(_gate.spine_plan)" in review
    assert _MOVED in review


def test_the_revise_and_patch_legs_read_the_followed_pulled_path_and_re_hand_the_revised_one():
    branch = _m_plus_branch(_compose())
    for label in ("plan-revise", "plan-patch"):
        leg = next(ln for ln in branch.splitlines() if f"label: '{label}'" in ln)
        assert "_planNow(_pulledEntry.planPath)" in leg, label
        assert _MOVED in leg, label
        assert "_pulledEntry.planPath)" not in leg.replace("_planNow(_pulledEntry.planPath)", ""), label
    assert "_pullPlan = _planSeen(_revised?.plan_rel) || _planNow(_pulledEntry.planPath);" in branch
    assert "{ _planRel = _pullPlan;" in branch
    assert "planPath: _pullPlan" in branch
    assert "_revised?.plan_rel || _pulledEntry.planPath" not in branch


def test_the_s_arm_repair_loop_follows_the_repaired_path():
    s = _compose()
    start = s.rindex(f"_gate.arm === '{ARM_S}') {{", 0, s.index("label: 'plan'"))
    branch = s[start : s.index(f"_gate.arm === '{ARM_M_PLUS}') {{")]
    assert "_planRel = _planSeen(_authored.plan_rel);" in branch
    assert "if (_pgRep?.plan_rel) _planRel = _planSeen(_pgRep.plan_rel);" in branch
    assert branch.index(OP_PREP_GATE) < branch.index("_planSeen(_pgRep.plan_rel)")


def test_a_leg_that_did_not_move_the_plan_keeps_the_authored_path():
    """The follower returns its argument for an unseen name, so an unmoved plan is handed on unchanged."""
    js = _follower_js(_compose())
    assert "_planNow = (p) => _planMoves[_planKey(p)] || p;" in js
