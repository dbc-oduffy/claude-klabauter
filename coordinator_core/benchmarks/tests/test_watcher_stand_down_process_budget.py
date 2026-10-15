"""Standing gate: the watcher stand-down op under the 500ms bar, by tree process time.

Same method as `test_workstream_complete_brief_process_budget.py`: net = (setup + call) minus the
setup-only run of the same driver, each the min over SAMPLES samples of K_INVOCATIONS runs, setup
and call sampled alternately. The op spawns nothing itself, so the net spawn count is pinned at 0.

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock.
THRESHOLD: 500ms is the brightline bar itself (DR-344).
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 5
SAMPLES = 5
BUDGET_MS = 500.0

_DRIVER = "coordinator_core.benchmarks.watcher_stand_down_full_path"


def _sample(workdir, *extra: str) -> dict:
    result = batched_process_time_ms([sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS)
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


@pytest.fixture(scope="module")
def stand_down_measurement(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("watcher_stand_down_budget")
    setups, fulls = [], []
    for _ in range(SAMPLES):
        setups.append(_sample(workdir, "--setup-only"))
        fulls.append(_sample(workdir))
    setup = min(setups, key=lambda r: r["process_time_ms"])
    full = min(fulls, key=lambda r: r["process_time_ms"])
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"watcher stand-down, net tree process time: {net_ms}ms, procs_per_call: {net_procs} "
        f"(setup+call {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"WATCHER-STAND-DOWN-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


def test_stand_down_is_under_the_500ms_bar(stand_down_measurement):
    m = stand_down_measurement
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


def test_stand_down_spawns_no_process_of_its_own(stand_down_measurement):
    m = stand_down_measurement
    assert m["net_procs"] <= 0, f"the op spawned: {m['detail']}"
