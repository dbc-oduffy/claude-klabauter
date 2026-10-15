"""criterion_judge_brief renders the same judge prompt as compose_criterion_judge for the same inputs."""

from coordinator_core.ops.review_mint.execute_review import (
    OperativeCriterion,
    _criterion_judge_prompt,
    _prompt_literal,
    compose_criterion_judge,
    criterion_judge_brief,
)
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _judge_review

_SCHEMAS = {**_STAGE_SCHEMAS, "judge-result": {"type": "object"}}
_BASE = dict(plan_path="docs/plans/example.md", run_base_sha="a" * 40)


def _head_prompt(review, **kw):
    _, prompt = _criterion_judge_prompt(
        review, falsifier=None, prompt_head="", criterion=None, host_degraded=False,
        evidence_path=None, evidence=None, em_owned_rows=None, ledger_charter=None, **kw,
    )
    return prompt


def test_defaults_match_the_pre_widening_prompt():
    review = _judge_review()
    prompt, schema = criterion_judge_brief(review, stage_schemas=_SCHEMAS, **_BASE)
    assert prompt == _head_prompt(review, **_BASE)
    assert isinstance(schema, dict)


def test_passthrough_keywords_render_as_compose_criterion_judge_renders_them():
    review = _judge_review()
    kwargs = dict(
        falsifier={"how": "run X", "expected_when_true": "green", "baseline_output": "red"},
        criterion=OperativeCriterion(statement="new statement", superseded="old statement"),
        evidence_path="state/evidence/example.yaml",
        em_owned_rows=["V9", "R7"],
    )
    prompt, _ = criterion_judge_brief(review, stage_schemas=_SCHEMAS, **_BASE, **kwargs)
    call = compose_criterion_judge(review, stage_schemas=_SCHEMAS, **_BASE, **kwargs)
    assert _prompt_literal(prompt) in call
    assert "recorded falsifier: how='run X'" in prompt
    assert "new statement" in prompt and "old statement" in prompt
    assert "row_evidence: state/evidence/example.yaml" in prompt
    assert "V9" in prompt and "R7" in prompt
    assert prompt != _head_prompt(review, **_BASE)
