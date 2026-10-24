"""A failed review.reverify list call is reported as status_before indeterminate, and grind withholds it."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import wake_digest
from coordinator_core.ops.dispatch_emit.landing_integrity import REVERIFY_INDETERMINATE
from coordinator_core.ops.dispatch_emit.tests.test_grind_compose import _compose
from coordinator_core.ops.review_mint import execute_review as er

_KW = dict(
    base_ref="abc123",
    prep_slice_id="prep",
    repo_root=None,
    plan_path="docs/plans/p.md",
    run_base_sha="deadbeef",
    prompt_head="HEAD",
    host_degraded=False,
    sc=lambda call, *_a: call,
)
_STEM = "run no tests and stop"


def _phase(**over) -> str:
    return er._reverify_phase(**{**_KW, **over})[1]


def test_single_prefix_prompt_carries_the_indeterminate_instruction():
    block = _phase()
    assert _STEM in block
    assert f'status_before \\"{REVERIFY_INDETERMINATE}\\"' in block or f'status_before "{REVERIFY_INDETERMINATE}"' in block
    assert "reverify_error" in block


def test_group_prompt_carries_the_indeterminate_instruction():
    block = _phase(group_prefixes=["g1", "g2"])
    assert _STEM in block
    assert "reverify_error" in block


def test_schema_declares_reverify_error_as_optional_string_or_null():
    prop = er._REVERIFY_SCHEMA["properties"]["reverify_error"]
    assert prop == {"type": ["string", "null"]}
    assert "reverify_error" not in er._REVERIFY_SCHEMA["required"]


def test_grind_withholds_an_indeterminate_fix_and_still_withholds_a_failed_one():
    script = _compose()
    assert f"['fail', '{REVERIFY_INDETERMINATE}'].includes(" in script
    assert ") : !_verifyOnly;" in script


def test_wake_digest_projection_carries_reverify_error():
    params = wake_digest.next_action_parts(
        has_commit_request=True,
        review_vars={
            "prep": "_reviewPrep", "wave": "_reviewWave", "delivery": "_deliveryVerdict",
            "bookkeeping_stem": "'stem'", "plan_id_literal": "null", "reverify": "_reviewReverify",
        },
        test_var=None, falsifier_var=None, verification_var="_verifications",
        test_absent_status="not_run",
    )[2]
    assert "reverify_error: _reviewReverify.reverify_error ?? null" in params
