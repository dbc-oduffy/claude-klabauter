"""compose_execute_review composes a review-wave op entry as one floor-tier op-relay call."""

import dataclasses

import pytest

from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _ratio_review


def _compose(review):
    return compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        executor_rows=9,
    )


def _with_op(review, **changes):
    op_agent = dataclasses.replace(
        review.review_wave[0], agent_type="dispatch.test_leg", op="dispatch.test_leg", model=None, effort=None, **changes
    )
    return dataclasses.replace(review, review_wave=[*review.review_wave, op_agent])


def test_model_only_wave_still_composes():
    assert "coordinator:op-relay" not in "\n".join(js for _, js in _compose(_ratio_review()))


def test_op_entry_composes_as_a_floor_tier_op_relay_call():
    script = "\n".join(js for _, js in _compose(_with_op(_ratio_review(), per="run")))
    assert "agentType: 'coordinator:op-relay'" in script
    assert "model: 'haiku'" in script
    assert "effort: 'low'" in script
    assert "coordinator-invoke dispatch.test_leg" in script
    assert "--bare" in script


def test_per_slice_op_entry_is_still_refused():
    with pytest.raises(ValueError, match="dispatch.test_leg"):
        _compose(_with_op(_ratio_review(), per="slice"))


def _compose_js(review, **kw):
    return "\n".join(
        js
        for _, js in compose_execute_review(
            review,
            stage_schemas=_STAGE_SCHEMAS,
            plan_path=kw.pop("plan_path", "docs/plans/example.md"),
            run_base_sha="a" * 40,
            declared_paths=["x.py"],
            executor_rows=9,
            **kw,
        )
    )


def test_gated_op_entry_emits_the_skipped_branch():
    script = _compose_js(_with_op(_ratio_review(), per="run", tiers=("full",)), tier_js="_tier")
    assert '["full"].includes(_tier) ? (' in script
    assert "excludes dispatch.test_leg" in script


def test_ungated_op_entry_emits_no_tier_gate():
    script = _compose_js(_with_op(_ratio_review(), per="run"), tier_js="_tier")
    assert '["full"].includes(_tier)' not in script
    assert "excludes dispatch.test_leg" not in script


def test_op_entry_is_verify_only_guarded_unless_prep_runs_in_wave():
    review = _with_op(_ratio_review(), per="run")
    assert "_verifyOnly ?" in _compose_js(review)
    assert "verify-only" in _compose_js(review)
    in_wave = _compose_js(review, prep_in_wave=True)
    assert "coordinator-invoke dispatch.test_leg" in in_wave
    assert "() => _verifyOnly ? (" not in in_wave


def test_single_quote_in_plan_path_cannot_break_the_relay_command():
    script = _compose_js(_with_op(_ratio_review(), per="run"), plan_path="docs/plans/it's.md")
    command = script[script.index("coordinator-invoke dispatch.test_leg") :].split("--bare", 1)[0]
    assert "it\\'\\\\\\'\\'s.md" in command
