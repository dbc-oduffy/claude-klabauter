"""
Tests for coordinator_core.plan_assemble.predicates.shared_booleans.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C9
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates.shared_booleans import (
    collapse_no_cross_repo_contract,
    collapse_scope_file_count,
)

_REPO_ROOT = Path("/repo")


def _context(scope=None, no_frontmatter=False) -> PredicateContext:
    frontmatter = None if no_frontmatter else {"scope": scope if scope is not None else []}
    return PredicateContext(
        repo_root=_REPO_ROOT,
        plan_path=Path("/repo/docs/plans/x.md") if not no_frontmatter else None,
        plan_frontmatter=frontmatter,
        plan_body="",
        sizing_object_path=None,
        sizing_frontmatter=None,
        resolved_route="spec-dispatch",
    )


class TestScopeFileCount:
    def test_at_threshold_true(self):
        result = collapse_scope_file_count(_context(scope=["a/", "b.py"]))
        assert result == {"scope_file_count": 2, "scope_file_count_le_2": True}

    def test_over_threshold_false(self):
        result = collapse_scope_file_count(_context(scope=["a/", "b.py", "c.py"]))
        assert result == {"scope_file_count": 3, "scope_file_count_le_2": False}

    def test_empty_scope_is_zero_and_true(self):
        result = collapse_scope_file_count(_context(scope=[]))
        assert result == {"scope_file_count": 0, "scope_file_count_le_2": True}

    def test_no_plan_frontmatter_is_undetermined(self):
        result = collapse_scope_file_count(_context(no_frontmatter=True))
        assert result["undetermined"] is True
        assert isinstance(result["reason"], str) and result["reason"]

    def test_no_scope_key_is_undetermined(self):
        context = PredicateContext(
            repo_root=_REPO_ROOT,
            plan_path=Path("/repo/docs/plans/x.md"),
            plan_frontmatter={"title": "no scope key here"},
            plan_body="",
            sizing_object_path=None,
            sizing_frontmatter=None,
            resolved_route="spec-dispatch",
        )
        result = collapse_scope_file_count(context)
        assert result["undetermined"] is True

    def test_literal_count_no_dedupe(self):
        result = collapse_scope_file_count(_context(scope=["a.py", "a.py", "b.py"]))
        assert result["scope_file_count"] == 3


class TestNoCrossRepoContract:
    def test_all_within_repo_root(self):
        result = collapse_no_cross_repo_contract(
            _context(scope=["coordinator_core/plan_assemble/", "docs/plans/x.md"])
        )
        assert result == {"crossing_paths": [], "no_cross_repo_contract": True}

    def test_relative_dotdot_crossing(self):
        result = collapse_no_cross_repo_contract(
            _context(scope=["coordinator_core/", "../DoE-claude/coordinator/docs/wiki/x.md"])
        )
        assert result["no_cross_repo_contract"] is False
        assert result["crossing_paths"] == ["../DoE-claude/coordinator/docs/wiki/x.md"]

    def test_absolute_path_outside_repo_root_crossing(self):
        outside_path = "/opt/sibling-repos/DoE-claude/coordinator/docs/wiki/x.md"  # abs-path-ok: synthetic fixture path, not a real host path
        result = collapse_no_cross_repo_contract(_context(scope=[outside_path]))
        assert result["no_cross_repo_contract"] is False
        assert result["crossing_paths"] == [outside_path]

    def test_absolute_path_inside_repo_root_not_crossing(self):
        result = collapse_no_cross_repo_contract(
            _context(scope=["/repo/coordinator_core/plan_assemble/"])
        )
        assert result["no_cross_repo_contract"] is True
        assert result["crossing_paths"] == []

    def test_empty_scope_not_crossing(self):
        result = collapse_no_cross_repo_contract(_context(scope=[]))
        assert result == {"crossing_paths": [], "no_cross_repo_contract": True}

    def test_no_plan_frontmatter_is_undetermined(self):
        result = collapse_no_cross_repo_contract(_context(no_frontmatter=True))
        assert result["undetermined"] is True
        assert isinstance(result["reason"], str) and result["reason"]

    def test_mixed_crossing_and_noncrossing_reports_only_crossing(self):
        result = collapse_no_cross_repo_contract(
            _context(scope=["coordinator_core/plan_assemble/", "../sibling/file.py"])
        )
        assert result["no_cross_repo_contract"] is False
        assert result["crossing_paths"] == ["../sibling/file.py"]
