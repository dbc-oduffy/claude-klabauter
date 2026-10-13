"""Standing gate: the no-script close under the 500ms bar, by tree process time.

The call is `dispatch.terminal_commit` with `plan_path` and `run_id` and no `script_path`, over a
landed-but-unclosed run (verdict from the review record, amend, mint, completion entry, flip,
commit, amend; the review launch stubbed). Same method as `test_close_review_process_budget.py`:
net = (setup + call) minus the setup-only run of the same driver, each the min over SAMPLES
samples of K_INVOCATIONS runs.

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock (CLAUDE.md,
The brightline). THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 500.0
NET_SPAWNS = 7.0
SPAWN_JITTER = 1.0  # procs_per_call is a mean over k and moves by ~0.5 between samples

_DRIVER = "coordinator_core.benchmarks.close_without_script_full_path"


def _sample(workdir, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _min_sample(workdir, *extra: str) -> dict:
    return min((_sample(workdir, *extra) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"])


@pytest.fixture(scope="module")
def measurement(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("close_without_script_budget")
    setup = _min_sample(workdir, "--setup-only")
    full = _min_sample(workdir)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"no-script close, net tree process time: {net_ms}ms, procs_per_call: {net_procs} "
        f"(setup+call {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"CLOSE-WITHOUT-SCRIPT-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


def test_no_script_close_is_under_the_500ms_bar(measurement):
    m = measurement
    assert m["net_ms"] > 0 and m["net_procs"] > 0, f"call cost not observed (full <= setup-only): {m['detail']}"
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


def test_no_script_close_spawn_count_is_pinned(measurement):
    m = measurement
    assert abs(m["net_procs"] - NET_SPAWNS) <= SPAWN_JITTER, f"spawn count moved from {NET_SPAWNS}: {m['detail']}"
