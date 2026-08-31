"""Every judgment point with `recommendation: null` must use the closed
`reason` enum (`insufficient-evidence` / `recommendation-forbidden`) that
`decision-object.schema.json`'s `if/then` conditional binds -- never free
prose.

Regression for cross-repo/archive/2026-08-13-doe-claude-em-baton-assemble-
reason-not-in-schema-enum.md: `baton_assemble` emitted prose (e.g. "Judgment
residue -- ...") on every null-recommendation judgment point while
`pickup_assemble` already conformed. This test fails if either side of that
producer/verifier pair regresses: a null-recommendation point emitting a
non-enum `reason`, or the enum itself narrowing below what these emitters use.
"""

from __future__ import annotations

_ALLOWED_NULL_RECOMMENDATION_REASONS = {"insufficient-evidence", "recommendation-forbidden"}


def _assert_null_recommendation_reasons_conform(judgment_points):
    for jp in judgment_points:
        if jp.get("recommendation") is not None:
            continue
        reason = jp.get("reason")
        assert reason in _ALLOWED_NULL_RECOMMENDATION_REASONS, (
            f"judgment point {jp.get('id')!r} has recommendation=None but "
            f"reason={reason!r}, not one of {_ALLOWED_NULL_RECOMMENDATION_REASONS}"
        )


def test_baton_assemble_handoff_judgment_points_conform():
    from coordinator_core.baton_assemble import _build_judgment_points

    points = _build_judgment_points(
        kind="handoff",
        dirty_tree_attribution={"degraded": True, "evidence": "test fixture"},
    )
    assert points, "expected at least one judgment point for kind=handoff"
    _assert_null_recommendation_reasons_conform(points)


def test_baton_assemble_spinoff_judgment_points_conform():
    from coordinator_core.baton_assemble import _build_judgment_points

    points = _build_judgment_points(
        kind="spinoff",
        dirty_tree_attribution={"degraded": True, "evidence": "test fixture"},
    )
    assert points, "expected at least one judgment point for kind=spinoff"
    _assert_null_recommendation_reasons_conform(points)
