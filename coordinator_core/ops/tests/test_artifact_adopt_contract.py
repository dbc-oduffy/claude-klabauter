"""Pins the artifact.adopt contract shared by the op and the post-write nudge."""

from __future__ import annotations

import pytest

from coordinator_core.ops.artifact_adopt_contract import (
    OP_NAME,
    AdoptResult,
    adopt_command,
    has_plan_producer_provenance,
    is_plan_path,
)


def test_op_name():
    assert OP_NAME == "artifact.adopt"


def test_adopt_command_with_write():
    assert adopt_command("docs/plans/x.md") == (
        "coordinator-invoke artifact.adopt '{\"path\":\"docs/plans/x.md\",\"write\":true}'"
    )


@pytest.mark.parametrize(
    "fm, expected",
    [
        ('title: a\nplan_id: "pln-x-abc123"\n', True),
        ("title: a\nplan_id: pln-x-abc123\n", True),
        ("plan_id: 'pln-x-abc123'  # c\n", True),
        ("plan_id: null\n", False),
        ("plan_id:\n", False),
        ("title: a\n", False),
        ('plan_id: "dlv-x-abc123"\n', False),
        ("  plan_id: pln-x\n", False),
        ("deliverable_id: pln-x\n", False),
    ],
)
def test_has_plan_producer_provenance(fm, expected):
    assert has_plan_producer_provenance(fm) is expected


@pytest.mark.parametrize(
    "path",
    ["docs/plans/2026-10-06-x.md", "docs\\plans\\2026-10-06-x.md"],
)
def test_is_plan_path_true(path):
    assert is_plan_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "docs/plans/2026-10-06-x.review.md",
        "docs/plans/2026-10-06-x.prior-art-check.md",
        "docs\\plans\\2026-10-06-x.review.md",
        "docs/plans/sub/x.md",
        "docs/plans/x.txt",
        "docs/plans/.md",
        "docs/plans/",
        "docs/research/x.md",
        "plans/x.md",
        "x/docs/plans/x.md",
        "README.md",
        "",
    ],
)
def test_is_plan_path_false(path):
    assert is_plan_path(path) is False


def test_adopt_result_keys():
    assert set(AdoptResult.__annotations__) == {
        "path", "type", "dry_run", "applied", "changed",
        "diff", "changes", "unfilled_required", "command",
    }
