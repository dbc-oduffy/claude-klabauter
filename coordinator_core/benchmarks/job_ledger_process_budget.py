"""`proc_census.read` process-time benchmark on this box (the real process table, real VRAM reader).

Reports process ms (`time.process_time`, never wall clock), spawn count and process count: a cold
call, then the median of `runs` warm calls.

Run: `python -m coordinator_core.benchmarks.job_ledger_process_budget [--runs N] [--enforce-bar]`.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

import pytest

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.group_em import proc_census

BAR_MS = 500.0
DEFAULT_RUNS = 5


def _call_once() -> dict:
    with pytest.MonkeyPatch.context() as spawn_mp, _count_spawns_attributed(spawn_mp) as spawns:
        t0 = time.process_time()
        reply = proc_census.read()
        total = (time.process_time() - t0) * 1000.0
    return {"process_ms": total, "spawns": len(spawns), "reply": reply}


def measure(runs: int = DEFAULT_RUNS) -> dict:
    """Cold call, then the median of `runs` warm calls."""
    cold = _call_once()
    samples = [_call_once() for _ in range(runs)]
    reply = samples[-1]["reply"]
    return {
        "system": reply["system"],
        "cold_process_ms": round(cold["process_ms"], 1),
        "process_ms": round(statistics.median(s["process_ms"] for s in samples), 1),
        "spawns": max(s["spawns"] for s in [cold, *samples]),
        "process_count": len(reply["processes"]),
        "unavailable": reply["unavailable"],
    }


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    report = measure(args.runs)
    report["bar_ms"] = BAR_MS
    print(json.dumps(report, indent=2, default=str))
    over = max(report["process_ms"], report["cold_process_ms"]) >= BAR_MS
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
