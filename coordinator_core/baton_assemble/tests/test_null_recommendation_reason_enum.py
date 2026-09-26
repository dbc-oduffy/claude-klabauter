
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
