"""An unattributed hunk (no run claims it) sharing lines with a run's own hunk no longer refuses
own-hunk-stage-refused: the path stages whole and the clean-overlay verify judges it. A proven peer's overlap
and an unreadable path still refuse."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401
    SEED,
    TARGET,
    _git,
    _land,
    _stage,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER_ADJACENT = SEED.replace("line5\n", "peer5\n")
OWN_ADJACENT = PEER_ADJACENT.replace("line6\n", "own6a\nown6b\n")


def test_an_unattributed_hunk_sharing_a_block_with_an_own_hunk_does_not_refuse_the_stage(repo):
    token = _stage(repo, PEER_ADJACENT, OWN_ADJACENT)

    out = _land(repo, token)

    assert out.get("refused") != "own-hunk-stage-refused", out
    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == OWN_ADJACENT


def test_the_filter_drops_only_unattributed_overlaps():
    rows = [
        ("a.py", None, "@@ -1,3 +1,4 @@"),
        ("b.py", "ask-peer", "@@ -9,1 +9,2 @@"),
        ("c.py", None, "unreadable"),
    ]

    assert land_run._drop_unattributed_overlaps(rows) == [
        ("b.py", "ask-peer", "@@ -9,1 +9,2 @@"),
        ("c.py", None, "unreadable"),
    ]
