"""The re-verify prompt points at comment_findings and provenance_findings."""

from __future__ import annotations

from coordinator_core.ops.review_mint import execute_review as er


def test_reverify_prompt_points_at_comment_findings_and_schema_keeps_them():
    assert "`comment_findings`" in er._REVERIFY_DERIVED_CLAUSE
    assert "comment_findings" in er._REVERIFY_SCHEMA["properties"]
    assert "`provenance_findings`" in er._REVERIFY_DERIVED_CLAUSE
    assert "provenance_findings" in er._REVERIFY_SCHEMA["properties"]
    assert er.COMMENT_FINDINGS_RULE in er._REVERIFY_DERIVED_CLAUSE
