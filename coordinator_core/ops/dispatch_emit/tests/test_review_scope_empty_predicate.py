"""review_floor.review_scope_empty truth table and review.reverify's `scope_files` reply field."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.review_floor import review_scope_empty
from coordinator_core.ops.tests.test_review_reverify import _list, world  # noqa: F401  (fixture)


@pytest.mark.parametrize(
    "declared, scope, expected",
    [
        (["a.py"], [], True),
        (["a.py"], ["a.py"], False),
        ([], [], False),
        ([], ["a.py"], False),
        (["a.py"], ["notes.md"], True),
        (["a.py"], ["b.py"], False),
    ],
)
def test_truth_table(declared, scope, expected) -> None:
    assert review_scope_empty(declared, scope) is expected


def test_list_mode_reports_the_manifest_files_as_scope_files_even_when_nothing_edited(world) -> None:  # noqa: F811
    root, original, _ = world
    out = _list(root)
    assert out["error"] is None
    assert out["edited"] == []
    assert out["scope_files"] == sorted(original)
    assert not review_scope_empty(["pkg/mod.py"], out["scope_files"])
