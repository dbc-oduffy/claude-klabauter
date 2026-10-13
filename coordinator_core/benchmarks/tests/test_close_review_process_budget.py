"""Standing gate: the close.review job under the 500ms bar, by tree process time.

The job is the capped unreceipted-ancestor range walk, run in one interpreter over a history-seeded
repo (thousands of `Session-Id` commits across a 1200-path pool through one `git fast-import`). Two
cases: the gap case (a receipted tip 50 commits below the run base) and the no-tip case (the range
walks to REVIEW_RANGE_CAP and truncates). Same method as `test_terminal_commit_process_budget.py`:
net = (setup + job) minus the setup-only run of the same driver, each the min over SAMPLES samples
of K_INVOCATIONS runs.

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

_DRIVER = "coordinator_core.benchmarks.close_review_full_path"


def _sample(workdir, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _min_sample(workdir, *extra: str) -> dict:
    return min((_sample(workdir, *extra) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"])


def _measure(tmp_path_factory, case: str, *flags: str) -> dict:
    workdir = tmp_path_factory.mktemp(f"close_review_budget_{case}")
    setup = _min_sample(workdir, "--setup-only", *flags)
    full = _min_sample(workdir, *flags)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"close.review job ({case}), net tree process time: {net_ms}ms, procs_per_call: {net_procs} "
        f"(setup+job {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"CLOSE-REVIEW-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


@pytest.fixture(scope="module")
def gap_measurement(tmp_path_factory):
    return _measure(tmp_path_factory, "gap")


@pytest.fixture(scope="module")
def no_tip_measurement(tmp_path_factory):
    return _measure(tmp_path_factory, "no-tip", "--no-tip")


def _assert_under_bar(m: dict) -> None:
    assert m["net_ms"] > 0 and m["net_procs"] > 0, f"job cost not observed (full <= setup-only): {m['detail']}"
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


def test_close_review_job_with_a_review_gap_is_under_the_500ms_bar(gap_measurement):
    _assert_under_bar(gap_measurement)


def test_close_review_job_with_a_capped_no_tip_walk_is_under_the_500ms_bar(no_tip_measurement):
    _assert_under_bar(no_tip_measurement)
