"""coordinator_core.benchmarks.hook_op_resolve_cost -- baseline instrument for
the two hook-door costs of ``hooks.preuse_write_dispatch``.

Axis A: modules imported when one ``hooks.*`` op is resolved through
``ipc.get_op_handler`` in a fresh ``-I`` interpreter (counts new
op modules under ``coordinator_core.hooks``; underscore-prefixed shared
helpers such as ``_envelope`` are reported separately, not counted).
Axis B: median ``time.process_time`` of a warm ``write_guards.engine.evaluate``
on an Edit payload, plus a cProfile top-N (cumulative) of one warm call.

Process time, never wall clock. This module fixes nothing; it reports.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_OP = "hooks.preuse_write_dispatch"
DEFAULT_SAMPLES = 15
DEFAULT_WARMUP = 3
DEFAULT_TOP = 20

_AXIS_A_SNIPPET = """
import json, sys
sys.path.insert(0, {root!r})
from coordinator_core import ipc
before = {{m for m in sys.modules if m.startswith("coordinator_core.hooks.")}}
handler = ipc.get_op_handler({op!r})
after = {{m for m in sys.modules if m.startswith("coordinator_core.hooks.")}}
print(json.dumps({{"resolved": handler is not None, "modules": sorted(after - before)}}))
"""


def measure_modules_imported(op: str = DEFAULT_OP) -> Dict[str, Any]:
    """Resolve ``op`` in a fresh ``-I`` subprocess; report new hooks.* modules."""
    code = _AXIS_A_SNIPPET.format(root=_REPO_ROOT, op=op)
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        **kwargs,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"axis A subprocess failed: {proc.stderr.strip()[-500:]}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    if not data["resolved"]:
        raise RuntimeError(f"axis A: {op} did not resolve")
    ops = [m for m in data["modules"] if not m.rsplit(".", 1)[-1].startswith("_")]
    helpers = [m for m in data["modules"] if m not in ops]
    return {"count": len(ops), "modules": ops, "shared_helpers": helpers}


def _edit_payload(directory: str) -> Dict[str, Any]:
    target = os.path.join(directory, "bench_target.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("alpha\n")
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": target,
            "old_string": "alpha",
            "new_string": "beta",
        },
        "session_id": "hook-op-resolve-cost-bench",
        "cwd": directory,
    }


def _top_rows(profiler: cProfile.Profile, top: int) -> List[Dict[str, Any]]:
    stats = pstats.Stats(profiler)
    rows = []
    for (filename, line, func), (cc, nc, tt, ct, _callers) in stats.stats.items():  # type: ignore[attr-defined]
        rows.append(
            {
                "file": filename,
                "line": line,
                "func": func,
                "ncalls": nc,
                "tottime_ms": round(tt * 1000, 3),
                "cumtime_ms": round(ct * 1000, 3),
            }
        )
    rows.sort(key=lambda r: r["cumtime_ms"], reverse=True)
    return rows[:top]


def measure_warm_evaluate(
    samples: int = DEFAULT_SAMPLES,
    warmup: int = DEFAULT_WARMUP,
    top: int = DEFAULT_TOP,
) -> Dict[str, Any]:
    """Median process-time ms of warm ``evaluate`` on an Edit, plus a profile."""
    from coordinator_core.write_guards.engine import evaluate

    with tempfile.TemporaryDirectory(prefix="hook-op-resolve-cost-") as tmp:
        payload = _edit_payload(tmp)
        for _ in range(warmup):
            evaluate(dict(payload), cwd=tmp)
        deltas: List[float] = []
        for _ in range(samples):
            t0 = time.process_time()
            evaluate(dict(payload), cwd=tmp)
            deltas.append((time.process_time() - t0) * 1000.0)
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            evaluate(dict(payload), cwd=tmp)
        finally:
            profiler.disable()
        profile = _top_rows(profiler, top)
    return {
        "median_ms": round(statistics.median(deltas), 3),
        "samples_ms": [round(d, 3) for d in deltas],
        "profile_top_cumulative": profile,
    }


def run(
    op: str = DEFAULT_OP,
    samples: int = DEFAULT_SAMPLES,
    warmup: int = DEFAULT_WARMUP,
    top: int = DEFAULT_TOP,
) -> Dict[str, Any]:
    """Run both axes and return the JSON-serialisable report."""
    a = measure_modules_imported(op)
    b = measure_warm_evaluate(samples=samples, warmup=warmup, top=top)
    return {
        "op": op,
        "hooks_modules_imported_resolving_one_op": a["count"],
        "hooks_modules_imported": a["modules"],
        "hooks_shared_helpers_imported": a["shared_helpers"],
        "warm_evaluate_process_ms_median": b["median_ms"],
        "warm_evaluate_samples_ms": b["samples_ms"],
        "warm_evaluate_profile_top20_cumulative": b["profile_top_cumulative"],
        "samples": samples,
    }


def main(argv: List[str] | None = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--op", default=DEFAULT_OP)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.op, args.samples, args.warmup, args.top), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
