"""halt_tree_check.broken_paths: an import miss caused by a peer's uncommitted module is not the run's defect."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_check


def test_owner_suffix_is_stripped_before_matching() -> None:
    detail = "imports helper from pkg.dep; pkg/dep.py is uncommitted in the worktree; owned by live run ask-1"
    assert halt_tree_check._foreign_uncommitted_target(detail, ["pkg/a.py"])
    assert not halt_tree_check._foreign_uncommitted_target(detail, ["pkg/dep.py"])
    assert not halt_tree_check._foreign_uncommitted_target("imports x; absent from HEAD", [])
