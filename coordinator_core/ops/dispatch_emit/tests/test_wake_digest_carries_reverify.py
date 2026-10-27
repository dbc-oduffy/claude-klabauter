"""The next-action digest carries the review re-verify verdict into inline_review, and only then."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import emit, wake_digest
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import REVERIFY_KEY

_BASE = {
    "prep": "_reviewPrep",
    "wave": "_reviewWave",
    "delivery": "_deliveryVerdict",
    "bookkeeping_stem": "'stem'",
    "plan_id_literal": "null",
}


def _params(review_vars):
    return wake_digest.next_action_parts(
        has_commit_request=True, review_vars=review_vars, test_var=None, falsifier_var=None,
        verification_var="_verifications", test_absent_status="not_run",
    )[2]


def test_reverify_binding_emits_the_key_with_only_the_fields_the_gate_reads():
    params = _params({**_BASE, "reverify": "_reviewReverify"})
    assert f"{REVERIFY_KEY}: (_reviewReverify ? {{ status_before:" in params
    assert "status_after: _reviewReverify.status_after ?? null" in params
    assert "failing: _reviewReverify.failing ?? []" in params
    assert "tests: v.tests ?? []" in params
    assert "sidecar" not in params.split(f"{REVERIFY_KEY}:", 1)[1].split("held:", 1)[0]


def test_without_the_binding_the_key_is_absent():
    assert REVERIFY_KEY + ":" not in _params(dict(_BASE))


def test_review_stage_vars_binds_reverify_on_the_plan_route():
    class _Review:
        integration = None

    out = emit.review_stage_vars(_Review(), bookkeeping_stem_literal="'s'", plan_id_literal="null")
    assert out["reverify"] == "_reviewReverify"
