"""The pre-integrity legs profile reports per-leg cost and can go red on per-touch wall.

Pinned: the JSON shape, that the tmp fixture's dirty files are found, the recorded baseline of this repo's
own tree, and the planted failure: a fixed per-touch delay pushes summed wall over `LAND_BUDGET_SECS`
while process time and spawns stay flat. No assertion reads box wall time (the delay runs on a virtual clock).
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import land_legs_pre_integrity_profile as prof
from coordinator_core.benchmarks import land_run_dirty_scan_bytes as fixture
from coordinator_core.ops.dispatch_emit import land_deadline

pytestmark = pytest.mark.cadence

RUN_ID = "ask-20261113T120000-abcdef"

#: Measured on this repo at 862726513a (newest land-legs run dir ask-20261113T233714-5a79b0, 72 literals),
#: default args: dirty_scan 1162 touches / ~94ms process, superseded_scan and foreign_withheld_scan 0 touches /
#: ~60-80ms process, 0 spawns, total ~235ms process. Spawns are the pin; the process ceiling leaves box slack.
BASELINE_SPAWNS = 0
BASELINE_PROCESS_MS_CEILING = 1000.0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


@pytest.fixture()
def repo(tmp_path):
    fx = fixture.build_fixture(tmp_path / "r", 25, 3)
    return tmp_path / "r", fx["paths"]


def _measure(repo, **kw):
    root, paths = repo
    return prof.measure(root, paths, RUN_ID, "state/sizings/x.yaml", **kw)


def test_report_shape(repo):
    res = _measure(repo)
    assert set(res["legs"]) == set(prof.LEGS)
    for leg in res["legs"].values():
        assert set(leg) == {"wall_ms", "process_ms", "spawns", "files_touched"}
    assert res["land_budget_secs"] == land_deadline.LAND_BUDGET_SECS
    assert res["total"]["files_touched"] == sum(l["files_touched"] for l in res["legs"].values())
    assert res["legs"]["dirty_scan"]["files_touched"] > 0
    assert res["wall_fits"] is True and res["process_fits"] is True


def test_per_touch_delay_turns_wall_red_while_process_and_spawns_stay_flat(repo):
    base = _measure(repo)
    touches = base["total"]["files_touched"]
    assert touches > 0
    delay = 2 * land_deadline.LAND_BUDGET_SECS / touches
    clock = _Clock()
    slow = _measure(repo, touch_delay_secs=delay, clock=clock, sleep=clock.sleep)
    assert slow["total"]["wall_ms"] > land_deadline.LAND_BUDGET_SECS * 1000.0
    assert slow["wall_fits"] is False
    assert slow["total"]["spawns"] == base["total"]["spawns"]
    assert slow["process_fits"] is True and slow["total"]["process_ms"] < land_deadline.LAND_BUDGET_SECS * 1000.0
    assert base["wall_fits"] is True


def test_recorded_baseline_of_this_repo_stays_under_the_ceilings():
    """Runs the default-args benchmark on this tree; pins spawns and process ceiling, never wall."""
    root = prof._REPO
    inputs = prof.default_inputs(root)
    res = prof.measure(root, inputs["literals"], inputs["run_id"], inputs["sizing_rel"])
    assert res["total"]["spawns"] == BASELINE_SPAWNS, res
    assert res["total"]["process_ms"] < BASELINE_PROCESS_MS_CEILING, res
    assert res["legs"]["dirty_scan"]["files_touched"] > 0, res
