"""
Tests for coordinator_core.ops._relative_link — the shared markdown-link
relativization seam both generate_exec_summary.py and
ceremony/renderers.py::render_plans_index_markdown route through.
"""

from __future__ import annotations

from coordinator_core.ops._relative_link import (
    normalize_repo_relative,
    relative_markdown_target,
)


def test_normalize_repo_relative_strips_leading_relative_segments():
    assert normalize_repo_relative("archive/foo.md") == "archive/foo.md"
    assert normalize_repo_relative("../archive/foo.md") == "archive/foo.md"
    assert normalize_repo_relative("../../archive/foo.md") == "archive/foo.md"
    assert normalize_repo_relative("./archive/foo.md") == "archive/foo.md"


def test_normalize_repo_relative_strips_lone_leading_single_dot():
    """Regression pin for coordinator-code-reviewer bd2f004c's nit: a lone
    leading `./` (not part of a `../` chain) is also stripped — pinning the
    single-dot case explicitly, not just as one assertion inside the
    leading-relative-segments test above."""
    assert normalize_repo_relative("./foo.md") == "foo.md"
    assert normalize_repo_relative("./archive/foo.md") == "archive/foo.md"


def test_relative_markdown_target_computes_correct_prefix_for_nested_out_path():
    assert relative_markdown_target("archive/foo.md", "docs/exec-summary.md") == "../archive/foo.md"
    assert (
        relative_markdown_target("state/handoffs/x.md", "docs/plans/INDEX.md")
        == "../../state/handoffs/x.md"
    )


def test_relative_markdown_target_is_idempotent_regardless_of_input_prefix():
    """A bare target and a target already (correctly-or-not) prefixed with a
    stray relative segment must converge to the same output — this is what
    closes the one-`../`-too-many defect: no upstream caller can silently
    layer a second relativization on top of a target harvested verbatim from
    a different source location."""
    bare = relative_markdown_target("archive/daily-summaries/x.md", "docs/exec-summary.md")
    stray_single = relative_markdown_target("../archive/daily-summaries/x.md", "docs/exec-summary.md")
    stray_double = relative_markdown_target("../../archive/daily-summaries/x.md", "docs/exec-summary.md")
    assert bare == stray_single == stray_double == "../archive/daily-summaries/x.md"


def test_relative_markdown_target_root_level_out_path_is_a_no_op():
    assert relative_markdown_target("archive/foo.md", "exec-summary.md") == "archive/foo.md"
