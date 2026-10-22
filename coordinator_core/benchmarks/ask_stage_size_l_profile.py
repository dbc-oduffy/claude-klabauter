"""Profile `dispatch_emit.ask_stage.stage` over the synthetic size-L fixture.

Prints process ms, wall ms, spawn count, a CPU-or-wait verdict and the top cumulative entries.
Run: `python -m coordinator_core.benchmarks.ask_stage_size_l_profile [--rows N] [--tokens K] [--top T]`.
Scratch lives in a temp dir this script removes.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

#: Process time at or above this share of wall time reads as CPU work; below it, the stage waited.
CPU_SHARE_FLOOR = 0.7


def _hot_step(stats: pstats.Stats, top: int) -> str:
    rows = []
    for (path, line, name), (_cc, _nc, tt, ct, _callers) in stats.stats.items():
        if "coordinator_core" in path.replace("\\", "/"):
            rows.append((ct, tt, f"{Path(path).name}:{line}:{name}"))
    rows.sort(reverse=True)
    return "\n".join(f"  cum={ct * 1000:9.1f}ms self={tt * 1000:9.1f}ms  {where}" for ct, tt, where in rows[:top])


def profile(rows: int, tokens: int, top: int) -> dict:
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit.ask_stage import stage
    from coordinator_core.ops.dispatch_emit.tests._size_l_plan import build_size_l_plan

    scratch = Path(tempfile.mkdtemp(prefix="ask-stage-size-l-"))
    mp = pytest.MonkeyPatch()
    try:
        managed = scratch / "managed"
        managed.mkdir()
        mp.setattr(warp_live, "managed_tempdir", lambda: managed)
        mp.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
        root = scratch / "repo"
        root.mkdir()
        repo_root, plan_rel, run_id = build_size_l_plan(root, rows=rows, live_tokens=tokens)
        plan_bytes = (repo_root / plan_rel).stat().st_size

        def call():
            return stage(repo_root, run_id=run_id, plan_rel=plan_rel)

        call()  # warm: lazy imports and first-touch caches are not the op
        with _count_spawns_attributed(mp) as spawns:
            profiler = cProfile.Profile()
            wall0, proc0 = time.perf_counter(), time.process_time()
            manifest = profiler.runcall(call)
            wall_ms = (time.perf_counter() - wall0) * 1000.0
            proc_ms = (time.process_time() - proc0) * 1000.0
        assert manifest.rows, "stage returned no rows: the figures below measure nothing"
        out = io.StringIO()
        stats = pstats.Stats(profiler, stream=out)
        try:
            steady = in_process_time_ms(call)["process_time_ms"]
        except ValueError:
            steady = None
        share = proc_ms / wall_ms if wall_ms else 0.0
        return {
            "plan_bytes": plan_bytes,
            "rows": len(manifest.rows),
            "live_tokens": tokens,
            "process_ms_profiled": round(proc_ms, 1),
            "process_ms_unprofiled": steady,
            "wall_ms": round(wall_ms, 1),
            "spawns": len(spawns),
            "verdict": "CPU" if share >= CPU_SHARE_FLOOR else "WAIT",
            "cpu_share": round(share, 2),
            "top": _hot_step(stats, top),
        }
    finally:
        mp.undo()
        rmtree_or_raise(scratch, label="ask-stage-size-l")


def main() -> None:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, default=14)
    ap.add_argument("--tokens", type=int, default=50)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    result = profile(args.rows, args.tokens, args.top)
    top = result.pop("top")
    for key, value in result.items():
        print(f"{key}: {value}")
    print("top cumulative (coordinator_core frames):")
    print(top)


if __name__ == "__main__":
    main()
