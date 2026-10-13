"""Standing gate: quick-wrap's brief under the 500ms bar, by tree process time.

The fixture seeds a real-scale history (HISTORY_COMMITS commits with Session-Id trailers),
because the brief's cost is history-depth-sensitive git work a one-commit repo cannot show. Same
method as `test_terminal_commit_process_budget.py`: net = (setup + call) minus the setup-only run
of the same driver, each the min over SAMPLES samples of K_INVOCATIONS runs, setup and call sampled alternately.
The fixture is built once before sampling, so setup-only is interpreter start plus import.

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock (CLAUDE.md,
The brightline). THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 5
SAMPLES = 5
BUDGET_MS = 500.0

_DRIVER = "coordinator_core.benchmarks.quick_wrap_brief_full_path"


def _sample(workdir, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _build(workdir) -> None:
    built = subprocess.run(
        [sys.executable, "-m", _DRIVER, str(workdir), "--build"], capture_output=True, text=True, **no_console_creationflags(),
    )
    assert built.returncode == 0, f"fixture build failed: {built.stderr}"


def _interleaved_mins(workdir) -> tuple[dict, dict]:
    """(setup-only, setup+call), each the min of SAMPLES samples taken alternately."""
    _build(workdir)
    setups, fulls = [], []
    for _ in range(SAMPLES):
        setups.append(_sample(workdir, "--setup-only"))
        fulls.append(_sample(workdir))

    def _least(rs):
        return min(rs, key=lambda r: r["process_time_ms"])

    return _least(setups), _least(fulls)


@pytest.fixture(scope="module")
def brief_measurement(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("quick_wrap_brief_budget")
    setup, full = _interleaved_mins(workdir)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"quick-wrap brief, net tree process time: {net_ms}ms, procs_per_call: {net_procs} "
        f"(setup+call {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"QUICK-WRAP-BRIEF-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


def test_quick_wrap_brief_is_under_the_500ms_bar(brief_measurement):
    m = brief_measurement
    assert m["net_ms"] > 0 and m["net_procs"] > 0, f"brief cost not observed (full <= setup-only): {m['detail']}"
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
