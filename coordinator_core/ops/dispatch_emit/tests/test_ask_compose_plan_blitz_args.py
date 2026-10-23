"""compose_ask_script: plan_blitz_args spread into planBlitz and emit-time writes seed."""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
# The bare token also sits in the scout reply schema (unconditional); only this plan-author clause is gated on args.
# Starts after "sizing's": the composer JS-escapes that apostrophe in the emitted script.
_SCOUT_EVIDENCE_CLAUSE = "`scout_evidence` entries, if it has any, before deriving the spine"


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


def _blitz_call(script):
    return script.split("await planBlitz(", 1)[1].split("\n", 1)[0]


def test_args_land_inside_the_planblitz_call_before_the_pinned_keys():
    args = {"scoutEvidence": ["a.md"], "pluginAgentsAvailable": True, "extra": 1}
    call = _blitz_call(_compose(plan_blitz_args=args))
    for key in args:
        assert f'"{key}"' in call
    assert call.index('"extra"') < call.index("mode: 'single'")
    assert call.index("mode: 'single'") < call.index("repoRoot: REPO_ROOT")


def test_none_and_empty_writes_leave_the_script_unchanged():
    base = _compose()
    assert _compose(plan_blitz_args=None, writes=()) == base
    assert "  let _writes = [];" in base
    assert _SCOUT_EVIDENCE_CLAUSE not in base


def test_writes_with_quote_and_backslash_round_trip_through_the_literal():
    writes = ['a"b.py', "c\\d.py"]
    script = _compose(writes=writes)
    line = next(ln for ln in script.splitlines() if ln.startswith("  let _writes = "))
    literal = re.match(r"  let _writes = (.*);$", line).group(1)
    assert json.loads(literal) == writes


def test_plan_author_prompt_reads_scout_evidence_when_args_given():
    assert _SCOUT_EVIDENCE_CLAUSE in _compose(plan_blitz_args={"k": 1})


def _blitz_calls(script):
    return [seg.split("\n", 1)[0] for seg in script.split("await planBlitz(")[1:]]


def test_both_planblitz_calls_carry_the_stage_ready_op_without_plan_path():
    calls = _blitz_calls(_compose())
    assert len(calls) == 2
    for call in calls:
        assert "readyOp: { op: 'dispatch.ask_stage'" in call
        assert "!_stagedManifest" in call
        ready = call.split("readyOp:", 1)[1].split("repoRoot:", 1)[0]
        # The reply schema legitimately declares plan_path (the owed-plan reply field); only the op params must not.
        params = ready.split("schema:", 1)[0]
        assert "plan_path" not in params
        assert "plan_path" in ready.split("schema:", 1)[1]
        # The plan branch omits the sizing-time writes (test_ask_compose_plan_stage_omits_writes).
        assert "writes" not in params
        for key in ("run_id", "cross_repo_approved", "session_id", "commit_sizing_path"):
            assert key in params


def test_a_stop_after_script_carries_no_ready_op():
    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN

    script = _compose(stop_after=STOP_AFTER_PLAN)
    assert "readyOp: {" not in script
