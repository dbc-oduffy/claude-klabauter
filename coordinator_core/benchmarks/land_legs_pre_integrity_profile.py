"""Wall, process time, spawns and file touches of the three `land_run` legs ahead of `land_run.integrity`.

Runs `_dirty_among`, `_superseded` and `foreign_halted_withheld` as `land_run` calls them, over a repo
root. File touches are counted by wrapping `os.stat`/`lstat`/`scandir`/`listdir`, `Path.exists`,
`Path.is_dir` and `open` (only the
outermost wrapper counts, so `Path.exists` -> `os.stat` is one touch). `touch_delay_secs` charges a fixed
delay per touch through `sleep`, so the wall metric that halts landings can be driven red while process
time and spawns stay flat.

    python coordinator_core/benchmarks/land_legs_pre_integrity_profile.py [--root PATH]

Prints one JSON object: per-leg `wall_ms`, `process_ms`, `spawns`, `files_touched`, their `total`, and
`wall_fits` / `process_fits` against `land_deadline.LAND_BUDGET_SECS`.
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import pytest  # noqa: E402

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed  # noqa: E402

LEGS = ("dirty_scan", "superseded_scan", "foreign_withheld_scan")

#: Literals when no run dir carries a `land-legs.json`: the declared writes of the plan that introduced this profile.
FALLBACK_LITERALS = (
    "coordinator_core/benchmarks/land_legs_pre_integrity_profile.py",
    "coordinator_core/benchmarks/tests/test_land_legs_pre_integrity_profile.py",
)
FALLBACK_SIZING = "state/sizings/2026-11/2026-11-13-land-legs-fit-budget-and-deadline-gate-release-122ce6.yaml"
FALLBACK_RUN_ID = "ask-20261113T000000-000000"


def _real_clock() -> float:
    return time.perf_counter()


class _Touches:
    """Counts outermost filesystem touches and charges `delay` through `sleep` on each."""

    def __init__(self, delay: float, sleep: Callable[[float], None]) -> None:
        self.count = 0
        self.depth = 0
        self.delay = delay
        self.sleep = sleep

    def wrap(self, real: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            if self.depth == 0:
                self.count += 1
                if self.delay:
                    self.sleep(self.delay)
            self.depth += 1
            try:
                return real(*args, **kwargs)
            finally:
                self.depth -= 1

        return wrapper


def newest_land_legs_run(root: Path) -> Optional[Path]:
    """The run dir under `RECORD_DIR_ROOT` whose `land-legs.json` was written last, or None."""
    from coordinator_core.ops.dispatch_emit import land_run

    best: Optional[tuple] = None
    try:
        entries = list(os.scandir(root / land_run.RECORD_DIR_ROOT))
    except OSError:
        return None
    for e in entries:
        try:
            m = os.stat(os.path.join(e.path, "land-legs.json")).st_mtime
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, Path(e.path))
    return best[1] if best else None


def default_inputs(root: Path) -> dict:
    """`{literals, run_id, sizing_rel, source}` for `root`: the newest land-legs run dir's files, else the fixed plan writes."""
    run = newest_land_legs_run(root)
    if run is None:
        return {"literals": list(FALLBACK_LITERALS), "run_id": FALLBACK_RUN_ID, "sizing_rel": FALLBACK_SIZING,
                "source": "fixed_plan_writes"}
    literals = []
    for dirpath, _dirs, files in os.walk(run):
        for f in files:
            literals.append(Path(dirpath, f).relative_to(root).as_posix())
    return {"literals": sorted(literals), "run_id": run.name, "sizing_rel": FALLBACK_SIZING,
            "source": f"run_dir:{run.name}"}


def measure(
    root: Path, literals: Sequence[str], run_id: str, sizing_rel: str, plan_rel: Optional[str] = None,
    touch_delay_secs: float = 0.0, clock: Callable[[], float] = _real_clock,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run the three legs once over `root` and report each leg's cost.

    `clock` and `sleep` are injectable so a test charges a per-touch delay on a virtual clock; process time
    always comes from `time.process_time()`. Foreign-scan paths are the dirty, non-ignored literals; when none
    are dirty every literal is asked about, so the leg is not skipped (`foreign_wanted` says which).
    """
    from coordinator_core.ops.dispatch_emit import land_deadline, land_run, run_token

    touches = _Touches(touch_delay_secs, sleep)
    legs: dict = {}
    real_open = builtins.open

    def run_leg(mp, name: str, fn: Callable):
        with _count_spawns_attributed(mp) as spawns:
            before = touches.count
            w0, p0 = clock(), time.process_time()
            value = fn()
            wall, proc = clock() - w0, time.process_time() - p0
        legs[name] = {
            "wall_ms": round(wall * 1000.0, 3), "process_ms": round(proc * 1000.0, 3),
            "spawns": len(spawns), "files_touched": touches.count - before,
        }
        return value

    foreign_wanted = "dirty"
    with pytest.MonkeyPatch.context() as mp:
        for fn in ("stat", "lstat", "scandir", "listdir"):
            mp.setattr(os, fn, touches.wrap(getattr(os, fn)))
        mp.setattr(Path, "exists", touches.wrap(Path.exists))
        mp.setattr(Path, "is_dir", touches.wrap(Path.is_dir))
        mp.setattr(builtins, "open", touches.wrap(real_open))
        changed, ignored, _head = run_leg(mp, "dirty_scan", lambda: land_run._dirty_among(root, list(literals)))
        run_leg(mp, "superseded_scan", lambda: land_run._superseded(root, run_id, sizing_rel, None, run_token))
        wanted = [p for p in literals if p in changed and p not in ignored]
        if not wanted:
            wanted, foreign_wanted = list(literals), "all_literals"
        run_leg(mp, "foreign_withheld_scan", lambda: land_run.foreign_halted_withheld(
            root, run_id, plan_rel, wanted, {},
        ))
    total = {k: round(sum(legs[n][k] for n in LEGS), 3) for k in ("wall_ms", "process_ms", "spawns", "files_touched")}
    budget_ms = land_deadline.LAND_BUDGET_SECS * 1000.0
    return {
        "legs": legs, "total": total, "land_budget_secs": land_deadline.LAND_BUDGET_SECS,
        "wall_fits": total["wall_ms"] <= budget_ms, "process_fits": total["process_ms"] <= budget_ms,
        "literals": len(literals), "foreign_wanted": foreign_wanted, "touch_delay_secs": touch_delay_secs,
    }


def main(argv: Optional[list] = None) -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=_REPO)
    args = ap.parse_args(argv)
    root = args.root.resolve()
    inputs = default_inputs(root)
    res = measure(root, inputs["literals"], inputs["run_id"], inputs["sizing_rel"])
    res.update(root=str(root), literals_source=inputs["source"])
    print(json.dumps(res, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
