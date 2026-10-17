"""Standing gate: a statusline render with the session-cost field, by process time.

The status line redraws on every assistant message across every concurrent
session; a slow render blanks bars fleet-wide. This gate measures one render per
cost-field state and the field's increment over the same render with no
transcript_path.

UNIT: process time and spawn count from `batched_process_time_ms` (k=K_INVOCATIONS),
never wall clock (CLAUDE.md, The brightline). A mode's render cost is the driver's
full run minus its `--setup-only` run (cache staging, interpreter start, driver
imports), each the min over SAMPLES samples so peer load inflates neither term.
The field's increment is that render cost minus the baseline mode's.

THRESHOLDS: total render under 100ms in every mode; field increment at most
10ms throttled, 30ms after an append, 40ms for a backlog render (one byte budget).
Spawns are reported beside the times and not asserted on.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 3
TOTAL_BUDGET_MS = 100.0
INCREMENT_BUDGET_MS = {"throttled": 10.0, "append": 30.0, "backlog": 40.0}

_DRIVER = "coordinator_core.benchmarks.statusline_render_full_path"


def _sample(workdir, mode: str, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, str(workdir), "--mode", mode, *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']} in mode {mode}: {result!r}"
    return result


def _min_sample(workdir, mode: str, *extra: str) -> dict:
    return min((_sample(workdir, mode, *extra) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"])


def _render_cost(workdir, mode: str) -> dict:
    setup = _min_sample(workdir, mode, "--setup-only")
    full = _min_sample(workdir, mode)
    return {
        "ms": round(full["process_time_ms"] - setup["process_time_ms"], 3),
        "procs": round(full["procs_per_call"] - setup["procs_per_call"], 3),
    }


@pytest.fixture(scope="module")
def measurements(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("statusline_render_budget")
    prepared = subprocess.run(
        [sys.executable, "-m", _DRIVER, str(workdir), "--prepare"], capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert prepared.returncode == 0, prepared.stderr
    costs = {mode: _render_cost(workdir, mode) for mode in ("baseline", *INCREMENT_BUDGET_MS)}
    for mode, cost in costs.items():
        increment = round(cost["ms"] - costs["baseline"]["ms"], 3)
        print(
            f"STATUSLINE-RENDER-BUDGET {mode}: render process time {cost['ms']}ms, "
            f"field increment {increment}ms, net procs {cost['procs']} "
            f"(k={K_INVOCATIONS}, min of {SAMPLES})"
        )
    return costs


@pytest.mark.parametrize("mode", ["baseline", *INCREMENT_BUDGET_MS])
def test_render_is_under_the_total_budget(measurements, mode):
    ms = measurements[mode]["ms"]
    assert ms < TOTAL_BUDGET_MS, f"{mode} render {ms}ms is over {TOTAL_BUDGET_MS}ms: {measurements!r}"


@pytest.mark.parametrize("mode", list(INCREMENT_BUDGET_MS))
def test_cost_field_increment_is_within_its_budget(measurements, mode):
    increment = measurements[mode]["ms"] - measurements["baseline"]["ms"]
    budget = INCREMENT_BUDGET_MS[mode]
    assert increment <= budget, (
        f"{mode}: cost field adds {round(increment, 3)}ms over baseline, budget {budget}ms: {measurements!r}"
    )
