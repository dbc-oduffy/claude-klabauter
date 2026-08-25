"""
Tests for `coordinator_core.ops.emit.deliverable_status.plan_review_verified`.

Spec backlink: docs/plans/2026-08-20-the-rungs-get-writers.md § C7 (AC15).
"""

from coordinator_core.ops.emit.deliverable_status import plan_review_verified


def test_plan_review_verified_true_when_review_verified_by_present():
    plan = {
        "status": "implemented",
        "review_verified_by": "code-reviewer-abc123",
        "review_verified_at": "2026-08-20T12:00:00Z",
        "review_verified_findings": "state/review-trail/findings/2026-08-20-foo.md",
    }
    assert plan_review_verified(plan) is True


def test_plan_review_verified_false_when_review_verified_by_absent():
    plan = {"status": "implemented"}
    assert plan_review_verified(plan) is False


def test_plan_review_verified_false_when_review_verified_by_explicitly_null():
    plan = {"status": "implemented", "review_verified_by": None}
    assert plan_review_verified(plan) is False
