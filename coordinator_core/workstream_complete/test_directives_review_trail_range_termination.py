
from __future__ import annotations

from coordinator_core.workstream_complete.directives_review import resolve_trail_range_tip


def test_resolve_tip_from_real_on_disk_shape_terminated_range():
    record = {
        "sha_range": "0227ea17..a4ef240b",
        "reviewer": "code-reviewer",
        "scope": "chain",
        "scope_kind": "diff",
        "session_id": "abc-123",
        "verdict": "ok",
        "diff_loc": 42,
        "workstream": "work/machine-a/2026-07-21",
    }
    tip, reason = resolve_trail_range_tip(record)
    assert tip == "a4ef240b"
    assert reason is None


def test_resolve_tip_from_real_on_disk_shape_caret_prefixed_start():
    record = {"sha_range": "0227ea17^..a4ef240b"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip == "a4ef240b"
    assert reason is None


def test_resolve_tip_rejects_unterminated_head_range():
    record = {"sha_range": "0227ea17..HEAD"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert reason is not None
    assert "HEAD" in reason


def test_resolve_tip_rejects_head_with_relative_suffix():
    record = {"sha_range": "0227ea17..HEAD~2"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "HEAD" in reason


def test_resolve_tip_rejects_dag_prefixed_range():
    record = {"sha_range": "dag:closing-handoff-segment"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "dag:" in reason


def test_resolve_tip_rejects_unparseable_range():
    record = {"sha_range": "not-a-range-at-all"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert reason is not None


def test_resolve_tip_rejects_missing_sha_range():
    record = {"reviewer": "code-reviewer"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "missing sha_range" in reason


def test_resolve_tip_honors_explicit_tip_field_forward_compat():
    record = {"sha_range": "0227ea17..HEAD", "sha_range_tip": "a4ef240b"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip == "a4ef240b"
    assert reason is None


def test_resolve_tip_rejects_explicit_head_tip_field():
    record = {"tip": "HEAD"}
    tip, reason = resolve_trail_range_tip(record)
    assert tip is None
    assert "HEAD" in reason


