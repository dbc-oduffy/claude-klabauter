"""Profile `dispatch_emit.ask_stage.stage` (or the whole `_handler`) over the synthetic size-L fixture.

Prints process ms, wall ms, spawn count, a CPU-or-wait verdict, a per-write-site wall split and the top
cumulative entries.
Run: `python -m coordinator_core.benchmarks.ask_stage_size_l_profile [--rows N] [--tokens K] [--top T]
[--dirty N] [--session-id SID] [--handler] [--plan PATH]`.
`--plan` copies PATH's bytes into the scratch fixture as the staged plan; it is never written back.
Under `--handler` the plan stamp reads `unchanged` on a fixture plan already `executing`, and a
`--plan` file keeps its own status. Scratch lives in a temp dir this script removes.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import importlib
import io
import pstats
import sys
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

#: (module, attribute) of each write site timed, by the name printed in the split.
WRITE_SITES = (
    ("ask_stage._write", "coordinator_core.ops.dispatch_emit.ask_stage", "_write"),
    ("atomic_replace.atomic_write_bytes", "coordinator_core.atomic_replace", "atomic_write_bytes"),
    ("locked_write.locked_rmw", "coordinator_core.locked_write", "locked_rmw"),
    ("claimed_write.replace_text", "coordinator_core.session.claimed_write", "replace_text"),
)


def _hot_step(stats: pstats.Stats, top: int) -> str:
    rows = []
    for (path, line, name), (_cc, _nc, tt, ct, _callers) in stats.stats.items():
        if "coordinator_core" in path.replace("\\", "/"):
            rows.append((ct, tt, f"{Path(path).name}:{line}:{name}"))
    rows.sort(reverse=True)
    return "\n".join(f"  cum={ct * 1000:9.1f}ms self={tt * 1000:9.1f}ms  {where}" for ct, tt, where in rows[:top])


def _wrap_write_sites(mp: pytest.MonkeyPatch) -> dict:
    """Wrap each write site, and every `coordinator_core` module that bound the original by name, with
    a wall timer. Returns `{site: [calls, wall_s]}`; nested calls (a site calling another) count in both."""
    totals: dict = {}
    for label, modname, attr in WRITE_SITES:
        owner = importlib.import_module(modname)
        original = getattr(owner, attr)
        totals[label] = [0, 0.0]

        def make(fn, cell):
            def timed(*a, **kw):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **kw)
                finally:
                    cell[0] += 1
                    cell[1] += time.perf_counter() - t0

            return timed

        wrapper = make(original, totals[label])
        for mod in list(sys.modules.values()):
            if getattr(mod, "__name__", "").startswith("coordinator_core") and getattr(mod, attr, None) is original:
                mp.setattr(mod, attr, wrapper)
    return totals


def _site_split(totals: dict) -> str:
    return "\n".join(
        f"  {label}: calls={calls} wall={wall * 1000:.1f}ms" for label, (calls, wall) in totals.items()
    )


def profile(
    rows: int,
    tokens: int,
    top: int,
    *,
    dirty: int = 0,
    session_id: str | None = None,
    handler: bool = False,
    plan: str | None = None,
) -> dict:
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import ask_stage, ask_stage_receipt
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
        repo_root, plan_rel, run_id = build_size_l_plan(
            root, rows=rows, live_tokens=tokens, dirty_files=dirty, session_id=session_id
        )
        plan_sha = None
        if plan:
            source = Path(plan).read_bytes()
            plan_sha = hashlib.sha256(source).hexdigest()[:16]
            (repo_root / plan_rel).write_bytes(source)
        plan_bytes = (repo_root / plan_rel).stat().st_size

        def stage_call():
            return ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=session_id)

        def handler_call():
            record = ask_stage.record_dir(repo_root, run_id)
            for name in (ask_stage_receipt.RECEIPT_NAME, "manifest.json"):
                (record / name).unlink(missing_ok=True)
            params = {"run_id": run_id, "plan_path": plan_rel, "writes": []}
            if session_id:
                params["session_id"] = session_id
            reply = ask_stage._handler(params, repo_root)
            assert "manifest_handle" in reply, f"handler refused: {reply}"
            return reply

        call = handler_call if handler else stage_call
        stage_call()  # warm: lazy imports and first-touch caches are not the op
        with _count_spawns_attributed(mp) as spawns:
            totals = _wrap_write_sites(mp)
            profiler = cProfile.Profile()
            wall0, proc0 = time.perf_counter(), time.process_time()
            result = profiler.runcall(call)
            wall_ms = (time.perf_counter() - wall0) * 1000.0
            proc_ms = (time.process_time() - proc0) * 1000.0
        stage_rows = len(result.rows) if not handler else rows
        if not handler:
            assert result.rows, "stage returned no rows: the figures below measure nothing"
        out = io.StringIO()
        stats = pstats.Stats(profiler, stream=out)
        try:
            steady = in_process_time_ms(call)["process_time_ms"]
        except ValueError:
            steady = None
        share = proc_ms / wall_ms if wall_ms else 0.0
        return {
            "mode": "handler" if handler else "stage",
            "plan_bytes": plan_bytes,
            "plan_source_sha256_16": plan_sha,
            "rows": stage_rows,
            "dirty_files": dirty,
            "live_tokens": tokens,
            "process_ms_profiled": round(proc_ms, 1),
            "process_ms_unprofiled": steady,
            "wall_ms": round(wall_ms, 1),
            "spawns": len(spawns),
            "verdict": "CPU" if share >= CPU_SHARE_FLOOR else "WAIT",
            "cpu_share": round(share, 2),
            "write_sites": "\n" + _site_split(totals),
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
    ap.add_argument("--dirty", type=int, default=0)
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--handler", action="store_true")
    ap.add_argument("--plan", default=None)
    args = ap.parse_args()
    result = profile(
        args.rows,
        args.tokens,
        args.top,
        dirty=args.dirty,
        session_id=args.session_id,
        handler=args.handler,
        plan=args.plan,
    )
    top = result.pop("top")
    for key, value in result.items():
        print(f"{key}: {value}")
    print("top cumulative (coordinator_core frames):")
    print(top)


if __name__ == "__main__":
    main()
