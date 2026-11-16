"""Workday-start launch profile: process time, spawn count and call counts on the redrive and
sentinels axes, the two named costs of a ~50s launch.

Axis `redrive`: `redrive_terminal_divergences` over the live corpus, called the way
`housekeeping/cycle.py :: _redrive_and_report` calls it; `cascade_terminal_sync` is a no-op, so the
run is read-only. Axis `sentinels`: the manifest's sentinels stage through `run_stages` with the real
dispatch table; the two mutating steps are stubbed.

Process time is `time.process_time` (redrive) and the summed ledger `process_ms` (sentinels); spawns are
`subprocess.Popen` constructions in this process plus the summed ledger `spawns`. Wall time is
attribution only. Run: `python -m coordinator_core.benchmarks.workday_start_launch_profile`.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
CEREMONY_DISPATCH_KEY = "workday_start"
SENTINELS_STAGE = "sentinels"
SENTINELS_RUNS = 5
MUTATING_SENTINELS = frozenset({"sentinel-doctor-sentinel-full", "sentinel-corpus-currency-probe"})


@contextlib.contextmanager
def _count_popen() -> Iterator[list[int]]:
    """Count `subprocess.Popen` constructions; `subprocess.run` builds a Popen, so it is counted once."""
    counter = [0]
    lock = threading.Lock()
    real = subprocess.Popen

    class _Counting(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            with lock:
                counter[0] += 1
            super().__init__(*args, **kwargs)

    subprocess.Popen = _Counting  # type: ignore[misc]
    try:
        yield counter
    finally:
        subprocess.Popen = real  # type: ignore[misc]


def _git_common_dir(repo_root: Path) -> Path:
    common = repo_root / ".git"
    if not common.is_dir():
        raise RuntimeError(f"workday_start_launch_profile: {repo_root} has no .git directory to use as the common dir")
    return common


def _live_terminal_handoff_metas(repo_root: Path) -> dict[str, dict]:
    """Terminal live handoffs keyed by repo-relative path: the `handoff_metas` the housekeeping cycle hands the redrive."""
    from coordinator_core.dag import _read_meta
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

    metas: dict[str, dict] = {}
    base = repo_root / "state" / "handoffs"
    for path in sorted(base.rglob("*.md")) if base.is_dir() else []:
        try:
            fm = _read_meta(str(path))
        except Exception:  # noqa: BLE001 -- an unreadable record is skipped as the cycle's scan skips it
            continue
        if fm and fm.get("deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT:
            metas[path.relative_to(repo_root).as_posix()] = fm
    return metas


def measure_redrive(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Run the redrive read-only; return call counts, process time and spawns of the redrive call alone."""
    from coordinator_core.ops import cascade_redrive, deliverable_cascade
    from coordinator_core.ops.cascade_backstop_sweep import plan_frontmatters

    common_dir = _git_common_dir(repo_root)
    metas = _live_terminal_handoff_metas(repo_root)
    plan_fms = plan_frontmatters(repo_root)

    calls = {"collector": 0, "read_meta": 0}
    real_collect = cascade_redrive._collect_live_candidates_for_kind

    def counting_collect(*args: Any, **kwargs: Any) -> Any:
        calls["collector"] += 1
        return real_collect(*args, **kwargs)

    def counting_read_meta(real: Callable[..., Any]) -> Callable[..., Any]:
        def read(*args: Any, **kwargs: Any) -> Any:
            calls["read_meta"] += 1
            return real(*args, **kwargs)

        return read

    saved = {
        (cascade_redrive, "_collect_live_candidates_for_kind"): real_collect,
        (cascade_redrive, "cascade_terminal_sync"): cascade_redrive.cascade_terminal_sync,
        (cascade_redrive, "_read_meta"): cascade_redrive._read_meta,
        (deliverable_cascade, "_read_meta"): deliverable_cascade._read_meta,
    }
    cascade_redrive._collect_live_candidates_for_kind = counting_collect
    cascade_redrive.cascade_terminal_sync = lambda *a, **k: {"advanced": [], "refused": [], "scan_incomplete": False}
    cascade_redrive._read_meta = counting_read_meta(saved[(cascade_redrive, "_read_meta")])
    deliverable_cascade._read_meta = counting_read_meta(saved[(deliverable_cascade, "_read_meta")])
    try:
        with _count_popen() as spawned:
            wall0, cpu0 = time.perf_counter(), time.process_time()
            result = cascade_redrive.redrive_terminal_divergences(common_dir, handoff_metas=metas, plan_fms=plan_fms)
            cpu_ms = (time.process_time() - cpu0) * 1000.0
            wall_ms = (time.perf_counter() - wall0) * 1000.0
    finally:
        for (module, name), original in saved.items():
            setattr(module, name, original)
    return {
        "terminal_handoffs": len(metas),
        "collector_calls": calls["collector"],
        "read_meta_calls": calls["read_meta"],
        "process_ms": round(cpu_ms, 1),
        "spawns": spawned[0],
        "wall_ms": round(wall_ms, 1),
        "scan_incomplete": bool(result.get("scan_incomplete")),
        "errors": len(result.get("errors") or []),
    }


def measure_sentinels(
    repo_root: Path = REPO_ROOT, today: _dt.date | None = None, *, concurrent: bool = False,
) -> dict[str, Any]:
    """Run the owed sentinels steps through `run_stages`; the mutating two are stubbed.

    `concurrent` passes the stage set `workday_start/run_spec.py` ships, so the figure is the default launch's.
    """
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.ceremony_common.run import runner
    from coordinator_core.temp_layout import make_managed_temp_dir
    from coordinator_core.workday_start import manifest

    day = today or _dt.date.today()
    steps = manifest.steps_for((SENTINELS_STAGE,), repo_root=repo_root, today=day)
    step_wall: dict[str, float] = {}

    def timed(kind: str, real: runner.Dispatcher) -> runner.Dispatcher:
        def dispatch(step: Any, **kwargs: Any) -> runner.ChildOutcome:
            start = time.perf_counter()
            try:
                if step.id in MUTATING_SENTINELS:
                    return runner.ChildOutcome(exit_code=0, process_ms=0.0, spawns=0, reason="stubbed")
                return real(step, **kwargs)
            finally:
                step_wall[step.id] = (time.perf_counter() - start) * 1000.0

        return dispatch

    table = {kind: timed(kind, fn) for kind, fn in runner.default_dispatch().items() if kind != "rule-child"}
    run_dir = make_managed_temp_dir("wsl-profile-", repo_root)
    real_flush = runner.flush_composition_record
    runner.flush_composition_record = lambda *a, **k: None
    try:
        with _count_popen() as spawned:
            wall0 = time.perf_counter()
            results = runner.run_stages(
                steps, ceremony=CEREMONY_DISPATCH_KEY, repo_root=repo_root, run_dir=run_dir, dispatch=table,
                concurrent_stages=frozenset({SENTINELS_STAGE}) if concurrent else frozenset(),
            )
            stage_wall_ms = (time.perf_counter() - wall0) * 1000.0
    finally:
        runner.flush_composition_record = real_flush
        rmtree_or_raise(run_dir, label="wsl-profile")
    statuses: dict[str, int] = {}
    for row in results:
        statuses[row.status] = statuses.get(row.status, 0) + 1
    return {
        "steps": len(results),
        "stubbed": sorted(s.id for s in steps if s.id in MUTATING_SENTINELS),
        "statuses": statuses,
        "process_ms": round(sum(r.process_ms or 0.0 for r in results), 1),
        "spawns": sum(r.spawns or 0 for r in results),
        "popen_spawns": spawned[0],
        "stage_wall_ms": round(stage_wall_ms, 1),
        "step_wall_ms_sum": round(sum(step_wall.values()), 1),
        "step_wall_ms": {sid: round(ms, 1) for sid, ms in step_wall.items()},
    }


def sentinels_spread(repo_root: Path = REPO_ROOT, *, concurrent: bool, runs: int = SENTINELS_RUNS) -> dict[str, Any]:
    """`runs` sentinels measurements; one run is peer-load noise, so the bar compares the median."""
    samples = [measure_sentinels(repo_root, concurrent=concurrent) for _ in range(runs)]
    ms = [s["process_ms"] for s in samples]
    return {
        "concurrent": concurrent,
        "process_ms_median": statistics.median(ms),
        "process_ms_runs": ms,
        "spawns_runs": [s["spawns"] for s in samples],
        "popen_spawns_runs": [s["popen_spawns"] for s in samples],
        "stage_wall_ms_runs": [s["stage_wall_ms"] for s in samples],
    }


def profile(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """`{"redrive": ..., "sentinels": ..., "sentinels_serial": ..., "sentinels_concurrent": ..., "sentinels_concurrency_adds_ms": ...}`.

    `sentinels` is one serial run; `sentinels_concurrency_adds_ms` is the same-tree, same-session concurrent
    median minus serial median, the figure that isolates what concurrency costs from tree state and peer load.
    """
    serial = sentinels_spread(repo_root, concurrent=False)
    concurrent = sentinels_spread(repo_root, concurrent=True)
    return {
        "redrive": measure_redrive(repo_root),
        "sentinels": measure_sentinels(repo_root),
        "sentinels_serial": serial,
        "sentinels_concurrent": concurrent,
        "sentinels_concurrency_adds_ms": round(concurrent["process_ms_median"] - serial["process_ms_median"], 1),
    }


def main() -> int:
    json.dump(profile(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
