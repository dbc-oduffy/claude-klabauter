"""compose_ask_script: a mechanical pull is accepted after one targeted revise; any other reply re-blitzes."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

pytestmark = pytest.mark.usefixtures("m_plus_known_arm")


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _pulled_block() -> str:
    script = compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    return script[script.index("_pulledEntry = ") : script.index("plan-blitz reported no ready plan")]


def _script(run_id: str) -> str:
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id=run_id,
        session_id=None, wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )


def test_readiness_verdict_is_returned_in_the_op_reply_never_via_a_shared_file():
    a, b = _script("run-a"), _script("run-b")
    for script in (a, b):
        assert "apm-readiness" not in script
        assert "readiness.json" not in script
        assert "plan.readiness_verdict" in script
    assert a != b


def test_revise_prompt_runs_the_readiness_verdict_op_with_pull_values():
    block = _pulled_block()
    assert "plan.readiness_verdict" in block
    assert "_pulledEntry.coverage" in block
    assert "_pulledEntry.reviews" in block
    assert "re-run the op with the plan alone" in block


def test_mechanical_ready_reply_is_accepted_without_a_reblitz():
    block = _pulled_block()
    branch = block.index("_revised?.pull_class === 'mechanical' && _revised?.verdict === 'ready'")
    accept = block.index("_planRel = _revised.plan_rel", branch)
    assert accept < block.index("else {", branch) < block.index("await planBlitz(")


def test_substantive_branch_still_reblitzes_once():
    block = _pulled_block()
    assert block.count("await planBlitz(") == 1
    assert block.count("label: 'plan-revise'") == 1
    assert "_blitz.regate = _reblitz" in block
    assert "_rePulled" in block and "_repeatBlocking = _fb" in block
