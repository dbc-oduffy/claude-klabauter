"""`groupem.goal_board_refresh` process-time benchmark over the vendored seed board.

Seeds the board under `<settings-home>/group-em/` in a managed temp dir from the vendored seed
fixture (one goal aged past `stale_after_hours`, two `task_map` rows: one unmapped, one `FIFA`), then
calls the registered op handler in-process, so the real `gem_home.resolve_home` default (no
configured key) and the real session-registry liveness read are on the measured path. Reports
process ms (`time.process_time`, never wall clock), spawn count and the flag kinds returned: a cold
call, then the median of `runs` warm calls. The seeded registry is empty, so every goal is
`no-live-owner`.

Baseline at 4b95d7471d: the op is absent (`unknown op groupem.goal_board_refresh`).

Run: `python -m coordinator_core.benchmarks.goal_board_refresh_process_budget [--runs N] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.group_em import goal_board

#: The row's end bar for this op (the brightline is 500ms; this op is held to 200).
BAR_MS = 200.0
DEFAULT_RUNS = 5
SEED_PATH = Path(__file__).resolve().parent.parent / "group_em" / "tests" / "fixtures" / "fleet-goal-board-seed.json"
EXPECTED_KINDS = frozenset({"stale", "no-live-owner", "unmapped-task"})
_STALE_STAMP = "2020-01-01T00:00:00Z"


def build_board() -> dict:
    """The vendored seed board with one stale goal and an unmapped plus a FIFA-mapped task."""
    board = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    board["goals"][0]["last_updated"] = _STALE_STAMP
    board["task_map"] = [
        {"em": "bench-em", "task_id": "t-unmapped", "goal": None},
        {"em": "bench-em", "task_id": "t-fifa", "goal": "FIFA"},
    ]
    return board


def seed_home(scratch: Path) -> Path:
    """Write the board where the default `resolve_home` finds it; returns the board path."""
    home = scratch / "settings-home" / "group-em"
    home.mkdir(parents=True)
    path = home / goal_board.BOARD_FILENAME
    path.write_text(json.dumps(build_board(), indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _call_once() -> dict:
    from coordinator_core.ops import group_em_goal_board_refresh as op

    spawn_mp = pytest.MonkeyPatch()
    with _count_spawns_attributed(spawn_mp) as spawns:
        t0 = time.process_time()
        reply = op._groupem_goal_board_refresh({})
        total = (time.process_time() - t0) * 1000.0
    return {
        "process_ms": total,
        "spawns": len(spawns),
        "git_spawns": sum(1 for s in spawns if s.argv and Path(s.argv[0]).stem.lower() == "git"),
        "spawn_argv": [s.argv for s in spawns],
        "reply": reply,
    }


def measure(runs: int = DEFAULT_RUNS) -> dict:
    """Cold call, then the median of `runs` warm calls over a freshly seeded board."""
    from coordinator_core.temp_layout import managed_tempdir

    scratch = Path(tempfile.mkdtemp(prefix="goal-board-bench-", dir=str(managed_tempdir())))
    try:
        with fixture_settings_home(scratch):
            seed_home(scratch)
            cold = _call_once()
            samples = [_call_once() for _ in range(runs)]
        mid = sorted(samples, key=lambda s: s["process_ms"])[len(samples) // 2]
        reply = mid["reply"]
        return {
            "cold_process_ms": round(cold["process_ms"], 1),
            "process_ms": round(statistics.median(s["process_ms"] for s in samples), 1),
            "spawns": max(s["spawns"] for s in [cold, *samples]),
            "git_spawns": max(s["git_spawns"] for s in [cold, *samples]),
            "spawn_argv": mid["spawn_argv"],
            "ok": reply.get("ok"),
            "errors": reply.get("errors"),
            "flag_kinds": sorted({f["flag"] for f in reply.get("flags") or []}),
            "flags": reply.get("flags"),
        }
    finally:
        rmtree_or_raise(scratch, label="goal-board-bench")


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when process ms >= {BAR_MS:.0f} or a git spawn occurs")
    args = ap.parse_args()
    report = measure(args.runs)
    report["bar_ms"] = BAR_MS
    print(json.dumps(report, indent=2, default=str))
    over = max(report["process_ms"], report["cold_process_ms"]) >= BAR_MS or report["git_spawns"] > 0
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
