"""Ask route: plan-review and revise prompts declare anchor premises and record verdicts via plan.premise_record."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script():
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


def test_review_and_revise_prompts_carry_anchor_sentence_before_readiness_op():
    script = _script()
    for needle in ("anchor_premises", "plan.premise_record", "anchor: <id>", ask_compose._ANCHOR_BRIEF_VERSION):
        assert script.count(needle) >= 2, needle
    from coordinator_core import ipc
    from coordinator_core.ops import plan_premise_carry  # noqa: F401  (registers the op)

    assert "plan.premise_record" in ipc._REGISTRY
    assert script.count("Declare `anchor_premises:`") == 2
    for chunk in script.split("Declare `anchor_premises:`")[1:]:
        assert chunk.index("plan.premise_record") < chunk.index("plan.readiness_verdict")
