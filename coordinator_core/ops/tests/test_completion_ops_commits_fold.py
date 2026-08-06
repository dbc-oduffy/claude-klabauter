"""
coordinator_core.ops.tests.test_completion_ops_commits_fold — direct unit
coverage for `_apply_commits_fold`'s flow-vs-block YAML normalization.

Review: code-reviewer (F5) — the module docstring identifies this exact
function's flow-style vs. block-style `commits:` handling as the fix for a
2026-07-22 production incident (silent corruption of a populated flow-style
list by appending block items beneath it). Prior test files
(test_completion_ops.py, tests/test_completion_ops_reconcile.py) only exercise
this path indirectly via end-to-end fixtures using empty-flow (`commits: []`)
or block-list forms — never a *populated* flow-style input, the exact shape
the incident hit. This file targets `_apply_commits_fold` directly.

Spec backlink: coordinator_core/ops/completion_ops.py § _apply_commits_fold
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.completion_ops import _apply_commits_fold


def test_populated_flow_style_normalizes_to_block_form() -> None:
    """A populated flow-style commits: ["a", "b"] fold appends new SHAs in block form.

    Byte-exact: existing items preserved in original order, new SHAs appended
    after them, all rewritten as block-form `  - "<sha>"` list items.
    """
    content = (
        "---\n"
        "status: pending-release\n"
        'commits: ["a", "b"]\n'
        "---\n"
        "body\n"
    )
    result = _apply_commits_fold(content, ["c"])

    assert result == (
        "---\n"
        "status: pending-release\n"
        "commits:\n"
        '  - "a"\n'
        '  - "b"\n'
        '  - "c"\n'
        "---\n"
        "body\n"
    )


def test_populated_flow_style_multiple_new_shas_preserve_order() -> None:
    content = (
        "---\n"
        "status: pending-release\n"
        'commits: ["x"]\n'
        "---\n"
        "body\n"
    )
    result = _apply_commits_fold(content, ["y", "z"])

    assert result == (
        "---\n"
        "status: pending-release\n"
        "commits:\n"
        '  - "x"\n'
        '  - "y"\n'
        '  - "z"\n'
        "---\n"
        "body\n"
    )


def test_unrecognized_commits_shape_raises_value_error() -> None:
    """A `commits:` line matching neither flow nor block form fails loud."""
    content = (
        "---\n"
        "status: pending-release\n"
        "commits: not-a-list-and-not-empty\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(ValueError, match="unrecognized commits: shape"):
        _apply_commits_fold(content, ["a"])
