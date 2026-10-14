"""Standing gate: `baton-chain-closure.py signal` under the 200ms bar at live corpus size.

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock (CLAUDE.md,
The brightline). THRESHOLD: 200ms is the one-process bar. Min over SAMPLES samples of
K_INVOCATIONS runs, as `test_close_background_process_budget.py` does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.baton_chain_closure_signal import build_corpus
from coordinator_core.benchmarks.process_time import batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 200.0
_CLI = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "baton-chain-closure.py"


@pytest.fixture(scope="module")
def measurement(tmp_path_factory):
    repo, member = build_corpus(tmp_path_factory.mktemp("bcc_signal"))
    spec = importlib.util.spec_from_file_location("bcc_under_budget", _CLI)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bcc_under_budget"] = module
    spec.loader.exec_module(module)
    verdict = module.chain_verdicts(repo, [member])[0]
    assert verdict.closed and verdict.signal, f"fixture chain did not close: {verdict!r}"
    cmd = [sys.executable, str(_CLI), "--repo", str(repo), "signal", member]
    best = None
    for _ in range(SAMPLES):
        r = batched_process_time_ms(cmd, k=K_INVOCATIONS)
        assert r["rc"] == 0, f"signal exited {r['rc']}: {r!r}"
        if best is None or r["process_time_ms"] < best["process_time_ms"]:
            best = r
    detail = (
        f"baton-chain-closure signal, process time: {best['process_time_ms']}ms, "
        f"procs_per_call: {best['procs_per_call']} (k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"BATON-CHAIN-CLOSURE-SIGNAL {detail}")
    return {**best, "detail": detail}


def test_signal_is_one_process_under_the_200ms_bar(measurement):
    m = measurement
    assert m["process_time_ms"] > 0, f"cost not observed: {m['detail']}"
    assert m["process_time_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
    assert m["procs_per_call"] == 1, f"signal spawned a child: {m['detail']}"
