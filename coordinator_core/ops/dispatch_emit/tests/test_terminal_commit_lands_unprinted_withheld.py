"""A run's own declared edit that a checkpoint withheld for want of a write-time print still lands at the
terminal commit, and a peer hunk the write baseline recorded in the same file stays out of it."""

from __future__ import annotations

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.dispatch_emit import run_write_prints
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401 -- fixture
    BOTH,
    PEER,
    RUN,
    SEED,
    TARGET,
    _git,
    _land,
    _stage,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _checkpoint_withholds_the_unprinted_path(root) -> None:
    reasons: dict[str, str] = {}
    blobs, withheld = checkpoint_hunks.checkpoint_own_blobs(root, RUN, [TARGET], "", reasons)
    assert (blobs, withheld) == ({}, [TARGET])
    assert reasons[TARGET] == checkpoint_hunks.NO_PRINT_REASON


def test_unprinted_withheld_path_lands_the_runs_hunk_and_keeps_the_peer_hunk_out(repo):
    token = _stage(repo, PEER, BOTH)
    assert not (run_write_prints.read(repo, RUN) or {})
    _checkpoint_withholds_the_unprinted_path(repo)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == SEED.replace("line10\n", "own10\n")
    assert (repo / TARGET).read_text(encoding="utf-8") == BOTH
    assert TARGET in {ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines()}


def test_unprinted_withheld_path_wholly_the_runs_own_lands_whole(repo):
    own = SEED.replace("line10\n", "own10\n")
    token = _stage(repo, SEED, own)
    _checkpoint_withholds_the_unprinted_path(repo)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == own
