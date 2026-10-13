"""Standing gate: the close.background job under the 500ms bar, by tree process time.

The job is the handoff sweep for the claimed batons, the pinboard-only orientation refresh,
the source memo's resolve (one commit) and the two index launchers, run in one interpreter.
Same method as `test_terminal_commit_process_budget.py`: net = (setup + job) minus the
setup-only run of the same driver, each the min over SAMPLES samples of K_INVOCATIONS runs.

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock (CLAUDE.md,
The brightline). THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
The launchers' detached children are stubbed out of the measurement (see the driver); the fixture's
sweep archives nothing, so that leg is a lower bound.
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 500.0

_DRIVER = "coordinator_core.benchmarks.close_background_full_path"


def _sample(workdir, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _min_sample(workdir, *extra: str) -> dict:
    return min((_sample(workdir, *extra) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"])


@pytest.fixture(scope="module")
def job_measurement(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("close_background_budget")
    setup = _min_sample(workdir, "--setup-only")
    full = _min_sample(workdir)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"close.background job, net tree process time: {net_ms}ms, procs_per_call: {net_procs} "
        f"(setup+job {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"CLOSE-BACKGROUND-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


def test_close_background_job_is_under_the_500ms_bar(job_measurement):
    m = job_measurement
    assert m["net_ms"] > 0 and m["net_procs"] > 0, f"job cost not observed (full <= setup-only): {m['detail']}"
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
