"""Known red: a live peer's hunk in a shared file is attributed to the landing run.

`run_landing._own_view` reverts only the hunks a peer's `kept.json` blob also made. A peer that is still
running has no `kept.json`, so its unrecorded hunk in a file this run also edited lands in this run's
commit. The attribution must come from the run's own write baseline instead.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_landing_parity import (  # noqa: F401 -- fixtures
    RUN_A,
    RUN_DIR_ROOT,
    X,
    _fire_b,
    _git,
    shared_repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.mark.xfail(
    strict=True,
    reason="own hunks are attributed from peers' kept.json blobs only, so a live peer's unrecorded hunk in a "
           "shared file lands in this run's commit; follow-up: derive the run's own hunk from its "
           "write_baseline and leave every other hunk dirty (no plan row owns this yet)",
)
def test_a_live_peers_unrecorded_hunk_in_a_shared_file_stays_out_of_the_commit(shared_repo):
    (shared_repo / "state" / "warp" / RUN_A / "kept.json").unlink()

    out = _fire_b("new", shared_repo)

    assert out["committed"] is True, out
    assert "A-EDIT" not in _git(["show", f"HEAD:{X}"], shared_repo)
    assert "A-EDIT" in (shared_repo / X).read_bytes().decode("utf-8")
