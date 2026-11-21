"""land_run refuses a commit set that is not exactly the run's recorded hunks, and judges import closure
against the committed image (HEAD plus the landing bytes), never the worktree."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import land_refusal_registry as reg
from coordinator_core.ops.dispatch_emit import land_run


TARGET = "pkg/shared.py"
OTHER = "pkg/other.py"
SEED = "".join(f"line{i}\n" for i in range(1, 13))
OWN = SEED.replace("line10\n", "own10\n")
PEER = SEED.replace("line1\n", "peer1\n")
BOTH = OWN.replace("line1\n", "peer1\n")


def test_the_refusal_reason_is_a_registered_head_integrity_refusal():
    entry = reg.classify(land_run.COMMIT_SET_NOT_RECORDED_HUNKS)
    assert entry is not None and entry.kind == "refusal" and entry.denies and entry.harm
