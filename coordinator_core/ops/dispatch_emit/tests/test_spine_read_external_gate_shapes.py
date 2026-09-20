"""Tests for claude-klabauter#43: two `external_gate` shapes this reader
used to silently ignore, dispatching a gated row as though it were open.

Shape 1 -- the gate is declared in plan FRONTMATTER carrying `row: <id>`.
Shape 2 -- the gate is a row-level sequence of plain strings, not mappings.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    AmbiguousExternalGateError,
    read_spine,
)

_HEADER = "# fixture plan\n\n## Tasks\n\n"


def _write_plan(tmp_path, body: str, *, frontmatter: str = ""):
    path = tmp_path / "plan.md"
    fm = f"---\n{frontmatter}\n---\n" if frontmatter else ""
    path.write_text(fm + _HEADER + "```yaml plan-tasks\n" + body + "\n```\n", encoding="utf-8")
    return path


def _basic_two_row_body():
    return (
        "- id: T1\n"
        "  title: gated row\n"
        "  surface: some/surface\n"
        "- id: T2\n"
        "  title: ordinary row\n"
        "  surface: some/other-surface\n"
    )


def test_frontmatter_gate_with_row_withholds_named_row(tmp_path):
    frontmatter = (
        "external_gate:\n"
        "  - id: claude-klabauter-inbox-delivery\n"
        "    row: T1\n"
        "    owner_repo: some-other-repo\n"
        "    requires: commit-in-owner-repo\n"
        "    cleared: false\n"
    )
    plan_path = _write_plan(tmp_path, _basic_two_row_body(), frontmatter=frontmatter)

    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"T2"}


def test_frontmatter_gate_cleared_true_dispatches_the_row(tmp_path):
    frontmatter = (
        "external_gate:\n"
        "  - id: claude-klabauter-inbox-delivery\n"
        "    row: T1\n"
        "    owner_repo: some-other-repo\n"
        "    cleared: true\n"
    )
    plan_path = _write_plan(tmp_path, _basic_two_row_body(), frontmatter=frontmatter)

    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"T1", "T2"}


def test_frontmatter_gate_naming_unknown_row_refuses(tmp_path):
    frontmatter = (
        "external_gate:\n"
        "  - id: claude-klabauter-inbox-delivery\n"
        "    row: NO-SUCH-ROW\n"
        "    cleared: false\n"
    )
    plan_path = _write_plan(tmp_path, _basic_two_row_body(), frontmatter=frontmatter)

    with pytest.raises(AmbiguousExternalGateError, match="NO-SUCH-ROW"):
        read_spine(plan_path)


def test_frontmatter_gate_entry_not_a_mapping_refuses(tmp_path):
    frontmatter = "external_gate:\n  - just-a-string\n"
    plan_path = _write_plan(tmp_path, _basic_two_row_body(), frontmatter=frontmatter)

    with pytest.raises(AmbiguousExternalGateError):
        read_spine(plan_path)


def test_frontmatter_gate_not_a_list_refuses(tmp_path):
    frontmatter = "external_gate: not-a-list\n"
    plan_path = _write_plan(tmp_path, _basic_two_row_body(), frontmatter=frontmatter)

    with pytest.raises(AmbiguousExternalGateError):
        read_spine(plan_path)


def test_frontmatter_gate_without_row_key_is_out_of_scope_and_skipped(tmp_path):
    frontmatter = "external_gate:\n  - id: plan-wide-gate\n    cleared: false\n"
    plan_path = _write_plan(tmp_path, _basic_two_row_body(), frontmatter=frontmatter)

    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"T1", "T2"}


def test_row_level_plain_string_gate_list_withholds_the_row(tmp_path):
    body = (
        "- id: C6\n"
        "  title: gated on named names\n"
        "  surface: some/surface\n"
        "  external_gate:\n"
        "    - host-retrieval-runtime\n"
        "    - bank-compatible-engine-store\n"
        "- id: C7\n"
        "  title: ordinary row\n"
        "  surface: some/other-surface\n"
    )
    plan_path = _write_plan(tmp_path, body)

    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C7"}


def test_row_level_mapping_gate_with_cleared_false_still_withholds(tmp_path):
    body = (
        "- id: C6\n"
        "  title: rewritten as mappings\n"
        "  surface: some/surface\n"
        "  external_gate:\n"
        "    - id: host-retrieval-runtime\n"
        "      cleared: false\n"
        "    - id: bank-compatible-engine-store\n"
        "      cleared: false\n"
        "- id: C7\n"
        "  title: ordinary row\n"
        "  surface: some/other-surface\n"
    )
    plan_path = _write_plan(tmp_path, body)

    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C7"}
