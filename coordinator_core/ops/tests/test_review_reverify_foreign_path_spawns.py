"""`open_probe`, given `live_runs`, never touches another live run's path and restores what it probed;
the `review.reverify` entry point supplies those `live_runs`.

`review.reverify`'s own entry point derives `live_runs` from `land_run.foreign_halted_withheld`, so a
foreign halted run's path is never probed (an empty `run_id` owns nothing, so every halted run is
foreign). Revert mode takes no `live_runs`: it asks `foreign_halted_withheld` directly and keeps a
path another run withholds (`peer-withheld`)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.run_ownership import LiveRun
from coordinator_core.ops.tests.test_review_prep import _base, _write, repo  # noqa: F401  (repo is a fixture)
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, _ledger, _row, world as rr_world,
)


WRITES = {"pkg/mine.py": b"probe-mine\n", "docs/plan.md": b"probe-plan\n"}


def test_review_prep_names_a_withheld_path_peer_held_and_keeps_it_in_review(repo) -> None:
    _write(repo, ["a/theirs.py"], 3)
    live = {"other": LiveRun(withheld=frozenset({"a/theirs.py"}))}
    reply = rp.review_prep(
        repo, base=_base(repo), paths=["a/theirs.py"], slice_prefix="p",
        plan_path="docs/plans/p.md", session_id="s", run_id="me", live_runs=live,
    )
    assert reply["error"] is None and reply["peer_held"] == ["a/theirs.py"]
    assert [f for s in reply["slices"] for f in s["files"]] == ["a/theirs.py"]
