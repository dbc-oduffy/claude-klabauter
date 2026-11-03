"""review.prep on a worktree with a concurrent `git add` writer stays under the brightline."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import review_prep_dirty_worktree as bench

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def test_worktree_prep_under_a_live_writer_stays_within_process_and_spawn_budget():
    result = bench.measure("worktree", 60, tree=True, writer=True)
    assert result["writer"]["iterations"] > 0, "the writer never ran; the measurement is vacuous"
    assert result["process_ms"] < bench.PROCESS_BAR_MS
    assert result["spawn_count"] <= bench.SPAWN_BOUND["worktree"]
