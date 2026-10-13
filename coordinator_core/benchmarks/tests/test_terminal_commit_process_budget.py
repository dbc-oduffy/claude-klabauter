"""Standing gate: dispatch.terminal_commit's full path under the 500ms bar, by tree process time.

The full path is the product commit, the review-stamp mint, the coded-stamp
commit and the implemented flip. `test_terminal_commit.py` used to bound it
with parent-only `time.process_time()`, which cannot see the git children (266ms
parent against 556ms tree at baseline); this gate measures the tree.

UNIT: process time and spawn count from `batched_process_time_ms`
(k=K_INVOCATIONS, job-object / kqueue / fork accounting), never wall clock
(CLAUDE.md, The brightline). Net = (setup + call) minus the setup-only run of
the same driver (`terminal_commit_full_path --setup-only`), each taken as the
min over SAMPLES samples so peer load inflates neither term.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
Process counts are reported beside it and not asserted on; on Windows each git
spawn is two processes (child plus console host).

HOOKS: the fixture installs no hook. DR-356 rules the commit hook chain out of
an op's own budget; a repo carrying hooks would read provisional here and the
hook figure belongs beside this one, never folded into it.
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 500.0

_DRIVER = "coordinator_core.benchmarks.terminal_commit_full_path"


def _sample(workdir, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _min_sample(workdir, *extra: str) -> dict:
    return min((_sample(workdir, *extra) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"])


@pytest.fixture(scope="module")
def full_path_measurement(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("terminal_commit_budget")
    setup = _min_sample(workdir, "--setup-only")
    full = _min_sample(workdir)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"terminal_commit full path, net tree process time: {net_ms}ms, "
        f"procs_per_call: {net_procs} (setup+call {full['process_time_ms']}ms / "
        f"{full['procs_per_call']} procs; setup-only {setup['process_time_ms']}ms / "
        f"{setup['procs_per_call']} procs; k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"TERMINAL-COMMIT-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


def test_terminal_commit_full_path_is_under_the_500ms_bar(full_path_measurement):
    m = full_path_measurement
    # Positive net is necessary, not sufficient: completion is the driver's rc==0
    # (last of k runs only, see batched_process_time_ms).
    assert m["net_ms"] > 0 and m["net_procs"] > 0, f"call cost not observed (full <= setup-only): {m['detail']}"
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
