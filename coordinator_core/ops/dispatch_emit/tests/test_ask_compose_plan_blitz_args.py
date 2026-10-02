"""compose_ask_script: plan_blitz_args spread into planBlitz and emit-time writes seed."""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
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
    assert "scout_evidence" not in base


def test_writes_with_quote_and_backslash_round_trip_through_the_literal():
    writes = ['a"b.py', "c\\d.py"]
    script = _compose(writes=writes)
    line = next(ln for ln in script.splitlines() if ln.startswith("  let _writes = "))
    literal = re.match(r"  let _writes = (.*);$", line).group(1)
    assert json.loads(literal) == writes


def test_plan_author_prompt_reads_scout_evidence_when_args_given():
    assert "scout_evidence" in _compose(plan_blitz_args={"k": 1})
