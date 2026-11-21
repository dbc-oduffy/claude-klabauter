"""`dispatch.land_blobs` finishes a landing by syncing each landed path into the worktree: a path ends equal to
HEAD or carrying only dirt rebased on HEAD, and a path that cannot be reconciled is reported and left byte-intact.

A peer edit lands in the window between the landing's file read and the sync, so each case writes the worktree
from a wrapper around `land_blobs._publish`, after the real ref move."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import land_blobs_worktree_sync as wsync


X = "pkg/shared.py"
RUN = "ask-20261114T100000-aaaaaa"
LINES = 120
OWN = "x_115 = 'OWN'"


def test_sync_plan_pairs_the_head_entry_with_the_staged_entry_and_a_deletion_with_none():
    spine = {"pkg": {"a.py": (0o100644, "a" * 40), "gone.py": (0o100644, "g" * 40)}}
    trees = {"pkg": {"a.py": (0o100644, "b" * 40)}, "": {}}

    plan = wsync.sync_plan(spine, trees, ["pkg/a.py"], ["pkg/gone.py"])

    assert plan == {"pkg/a.py": ((0o100644, "a" * 40), (0o100644, "b" * 40)),
                    "pkg/gone.py": ((0o100644, "g" * 40), None)}
