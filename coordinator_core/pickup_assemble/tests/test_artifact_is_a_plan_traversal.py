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
