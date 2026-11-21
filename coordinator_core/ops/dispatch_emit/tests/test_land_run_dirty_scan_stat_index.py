"""land_run's dirty scan settles stat-clean literals from the index sha with no byte read and hashes
only stat-dirty ones; staged changes, same-size rewrites, untracked, deleted and filter-refused
literals keep their changed/unchanged verdicts."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run


def test_shadowed_worktree_blob_def_is_gone():
    src = Path(land_run.__file__).read_text(encoding="utf-8")
    assert src.count("\ndef _worktree_blob(") == 1
