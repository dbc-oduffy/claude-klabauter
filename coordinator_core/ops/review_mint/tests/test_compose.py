"""Tests for ``coordinator_core.ops.review_mint.compose.compose``.

Spec: ``docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md`` task C2.
Pure unit tests against inline `Stage` fixtures -- no file I/O, no fragment
parsing (that is C1's `roster.parse_stages`, exercised separately).
"""

import pytest

from coordinator_core.ops.review_mint.compose import ComposeError, compose
from coordinator_core.ops.review_mint.roster import Stage

_PROMPT = "Review this plan."
_PHASE_TITLE = "Review"


def _disarmed_policy(stage, index, results):
    return ""


def _abort_policy(stage, index, results):
    var = results[0][1]
    return (
        f"  if ({var}.verdict) {{\n"
        f"    return {{ blocking_agent: {var!r}, verdict: {var}.verdict, "
        f"reason: {var}.reason, sidecar_path: {var}.sidecar_path }};\n"
        "  }"
    )


def test_single_agent_stage_emits_serial_call():
    stages = [Stage(agents=["coordinator:code-reviewer"], gate=False)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy)
    assert len(out) == 1
    title, block = out[0]
    assert title == _PHASE_TITLE
    assert "await agent(" in block
    assert "parallel(" not in block
    assert "model:" not in block
    assert "schema:" not in block


def test_multi_agent_stage_emits_parallel_call():
    stages = [
        Stage(agents=["coordinator:code-reviewer", "coordinator:staff-eng"], gate=False)
    ]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy)
    title, block = out[0]
    assert "await parallel([" in block
    assert block.count("() => agent(") == 2
    assert "model:" not in block


def test_stage_order_and_unique_titles_preserved():
    stages = [
        Stage(agents=["coordinator:prior-art-checker"], gate=True),
        Stage(agents=["coordinator:code-reviewer", "coordinator:staff-eng"], gate=False),
        Stage(agents=["coordinator:review-integrator"], gate=False),
    ]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy)
    titles = [t for t, _ in out]
    assert titles == ["Review 1/3", "Review 2/3", "Review 3/3"]
    assert len(set(titles)) == len(titles)


def test_gate_stage_serial_carries_schema_and_calls_gate_policy():
    stages = [Stage(agents=["coordinator:prior-art-checker"], gate=True)]
    seen = {}

    def policy(stage, index, results):
        seen["stage"] = stage
        seen["index"] = index
        seen["results"] = results
        return "  // abort here"

    out = compose(stages, _PROMPT, _PHASE_TITLE, policy)
    _, block = out[0]
    assert "schema:" in block
    assert "verdict" in block and "reason" in block and "sidecar_path" in block
    assert "const reviewStage0Result = await agent(" in block
    assert "// abort here" in block
    assert seen["index"] == 0
    assert seen["results"] == [("coordinator:prior-art-checker", "reviewStage0Result")]


def test_gate_stage_parallel_indexes_result_per_agent():
    stages = [
        Stage(
            agents=["coordinator:prior-art-checker", "coordinator:docs-checker"],
            gate=True,
        )
    ]
    seen = {}

    def policy(stage, index, results):
        seen["results"] = results
        return ""

    out = compose(stages, _PROMPT, _PHASE_TITLE, policy)
    _, block = out[0]
    assert "const reviewStage0Result = await parallel([" in block
    assert block.count("schema:") == 2
    assert seen["results"] == [
        ("coordinator:prior-art-checker", "reviewStage0Result[0]"),
        ("coordinator:docs-checker", "reviewStage0Result[1]"),
    ]


def test_disarmed_gate_policy_emits_no_branch():
    stages = [Stage(agents=["coordinator:prior-art-checker"], gate=True)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy)
    _, block = out[0]
    assert "return" not in block


def test_abort_policy_returns_ac5_shaped_object():
    stages = [Stage(agents=["coordinator:prior-art-checker"], gate=True)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _abort_policy)
    _, block = out[0]
    assert "return {" in block
    for field in ("blocking_agent", "verdict", "reason", "sidecar_path"):
        assert field in block


def test_non_gate_agent_completes_without_schema_or_branch():
    """AC6: an agent with no blocking verdict still completes and never
    triggers a branch -- modelled here as a non-gate stage, since whether an
    agent CAN block is `blocking_verdicts` data C2 never reads (see module
    docstring) -- that discrimination is the caller's `gate_policy` closure,
    exercised in `test_roundtrip.py` (C5) against the real fragment."""
    stages = [Stage(agents=["coordinator:docs-checker"], gate=False)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy)
    _, block = out[0]
    assert "schema:" not in block
    assert "return" not in block


def test_empty_stage_list_refuses_loudly():
    with pytest.raises(ComposeError):
        compose([], _PROMPT, _PHASE_TITLE, _disarmed_policy)


def test_gate_stage_schema_carries_run_nonce_and_marks_it_required():
    """AC12 leg 2: the shared schema literal grows a `run_nonce` field,
    required alongside `verdict`, for every gate-stage agent call --
    unconditionally, since dispatch.emit's own gate stage reuses the same
    literal (see compose.py module docstring)."""
    stages = [Stage(agents=["coordinator:prior-art-checker"], gate=True)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy)
    _, block = out[0]
    assert "run_nonce: { type: 'string' }" in block
    assert "required: ['verdict', 'run_nonce']" in block


def test_gate_stage_prompt_carries_the_run_nonce_when_supplied():
    stages = [Stage(agents=["coordinator:prior-art-checker"], gate=True)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy, run_nonce="deadbeef01234567")
    _, block = out[0]
    assert "run_nonce: deadbeef01234567" in block


def test_non_gate_stage_prompt_never_carries_a_run_nonce():
    """AC12 anti-scope: only a gate stage has a verdict to refuse -- a
    non-gate stage's prompt is untouched even when the caller supplies a
    run_nonce (mirrors run-report family exemption at the stage-type
    level, not per-agent)."""
    stages = [Stage(agents=["coordinator:review-integrator"], gate=False)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy, run_nonce="deadbeef01234567")
    _, block = out[0]
    assert "run_nonce" not in block


def test_run_nonce_omitted_by_default_leaves_prompt_unmodified():
    """A caller that never passes run_nonce (dispatch.emit's already-landed
    C4 call site) still gets `run_nonce` in the schema literal (shared
    constant), but the prompt itself carries no injected value."""
    stages = [Stage(agents=["coordinator:prior-art-checker"], gate=True)]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _disarmed_policy)
    _, block = out[0]
    assert "run_nonce:" in block  # schema field, unconditional
    assert "\\n\\nrun_nonce:" not in block  # but no injected literal value


def test_no_model_key_anywhere():
    stages = [
        Stage(agents=["coordinator:prior-art-checker"], gate=True),
        Stage(agents=["coordinator:code-reviewer", "coordinator:staff-eng"], gate=False),
    ]
    out = compose(stages, _PROMPT, _PHASE_TITLE, _abort_policy)
    for _, block in out:
        assert "model:" not in block
