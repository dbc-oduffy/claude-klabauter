"""
coordinator_core.pickup_assemble.tests.test_artifact_is_a_plan_traversal

Purpose: pins `_artifact_is_a_plan`'s normalization against a `..`-traversal
misclassification.

Review: coordinator:code-reviewer — `artifact_path.replace(chr(92),
"/").lstrip("./")` strips a character SET, not a literal prefix: a
traversal-shaped input like `"../../docs/plans/x.md"` has its leading run of
`.`/`/` characters collapsed away entirely, leaving `"docs/plans/x.md"`,
which then wrongly satisfies `startswith(_plan_dirs())` and is classified as
an in-tree plan. Through `brief()` this was masked by
`_repo_relative_artifact_path`'s traversal rejection upstream, but
`coordinator_core/pickup_assemble/stamp_check.py`'s standalone
`pickup-assemble stamp-check <plan-path>` CLI verb calls
`compute_execution_stamp_match` (and therefore `_artifact_is_a_plan`)
directly from argv, with no such upstream guard — this path is live, not
dead, contrary to the premise an earlier review pass reached before
`stamp_check.py`'s second call site was found.
"""
from __future__ import annotations

import coordinator_core.pickup_assemble as pa


def test_traversal_path_is_not_classified_as_a_plan():
    assert pa._artifact_is_a_plan("../../docs/plans/x.md") is False


def test_absolute_style_leading_slash_traversal_is_not_classified_as_a_plan():
    assert pa._artifact_is_a_plan("/../docs/plans/x.md") is False


def test_leading_dot_slash_is_still_stripped_and_classified():
    assert pa._artifact_is_a_plan("./docs/plans/x.md") is True


def test_plain_in_tree_plan_path_is_classified():
    assert pa._artifact_is_a_plan("docs/plans/x.md") is True


def test_backslash_traversal_is_not_classified_as_a_plan():
    assert pa._artifact_is_a_plan("..\\..\\docs\\plans\\x.md") is False
