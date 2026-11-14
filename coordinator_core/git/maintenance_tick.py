"""Timed trigger for the lock-holding git maintenance, hosted on the warm engine's idle tick.

With `gc.auto=0` and `maintenance.auto=false` the engine's maintenance is the only repacker, so
it must run without a human trigger. `on_idle_tick` asks one question per served repo -- "is the
daily (24h) or weekly (7d) stamp stale?" -- from a stamp stat, and launches ONE detached child for
the due tiers. The child runs them in order, each holding `object_store_lock` (store_hygiene's
daily body, `git_maintenance.run_tier("weekly")`), after a busy check.

Invariants:
  - The tick thread spawns nothing and samples no load when nothing is owed; the busy check
    (`store_hygiene._box_busy`, which may sleep a sampling gap) and all real work run in the child.
  - The stamp is claimed (touched) at launch, so a slow child is not relaunched. A child that finds
    the box busy, the lock held or the run failed backdates the stamp to retry after `RETRY_S`, not a whole interval.
  - At most one child launch per tick; repos are staggered across ticks.
  - Stamps live under the repo's git common dir.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir

#: Seconds between runs of each tier.
DAILY_INTERVAL_S = 24 * 3600
WEEKLY_INTERVAL_S = 7 * 24 * 3600
#: Retry delay after a child skipped a tier (box busy, lock held).
RETRY_S = 3600
#: In-process gate: a repo's stamps are re-stat'ed at most this often.
CHECK_INTERVAL_S = 300.0

TIERS = ("daily", "weekly")
_INTERVALS = {"daily": DAILY_INTERVAL_S, "weekly": WEEKLY_INTERVAL_S}
_STAMP_DIR = "coordinator"

_log = logging.getLogger(__name__)
_next_check: dict[Path, float] = {}


def stamp_path(common_dir: Path, tier: str) -> Path:
    """The last-run stamp of `tier` under `common_dir`; its mtime is the claim/run time."""
    return Path(common_dir) / _STAMP_DIR / f"maintenance-tick-{tier}.stamp"


def _owed(common_dir: Path, tier: str, now: float) -> bool:
    try:
        return now - stamp_path(common_dir, tier).stat().st_mtime >= _INTERVALS[tier]
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _claim(common_dir: Path, tier: str) -> bool:
    path = stamp_path(common_dir, tier)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return True
    except OSError:
        return False


def _backdate(common_dir: Path, tier: str) -> None:
    """Make `tier` owed again `RETRY_S` from now."""
    path = stamp_path(common_dir, tier)
    when = time.time() - _INTERVALS[tier] + RETRY_S
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (when, when))
    except OSError:
        pass


def _spawn(repo: Path, tiers: list[str]) -> bool:
    """Launch the detached child; True when the spawn did not fail. Test seam."""
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine_root = Path(__file__).resolve().parents[2]
    return spawn_detached(
        str(engine_root), str(Path(__file__).resolve()), ["--repo", str(repo), "--tiers", ",".join(tiers)]
    )


def on_idle_tick(repos: Iterable[Path]) -> bool:
    """Launch the due tiers for at most one repo; True when a child was launched.

    A not-owed tick is a few stats and spawns nothing."""
    now_mono = time.monotonic()
    now = time.time()
    seen: set[Path] = set()
    for raw in repos:
        try:
            repo = Path(raw).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if repo in seen:
            continue
        seen.add(repo)
        if now_mono < _next_check.get(repo, float("-inf")):
            continue
        _next_check[repo] = now_mono + CHECK_INTERVAL_S
        try:
            if not (repo / ".git").exists():
                continue
            common_dir = resolve_git_common_dir(repo)
            due = [t for t in TIERS if _owed(common_dir, t, now)]
            if not due:
                continue
            claimed = [t for t in due if _claim(common_dir, t)]
            if not claimed:
                continue
            if _spawn(repo, claimed):
                return True
            for tier in claimed:
                _backdate(common_dir, tier)
        except Exception as exc:  # noqa: BLE001 -- the tick never fails on a maintenance error
            _log.warning("git maintenance tick failed for %s: %s", repo, exc)
    return False


def run_due(repo: Path, tiers: Iterable[str]) -> int:
    """Child body: run each tier in order; a busy box, held lock or failed run backdates its stamp. Returns
    the worst exit code."""
    from coordinator_core.git import store_hygiene
    from coordinator_core.ops.git_maintenance import run_tier

    common_dir = resolve_git_common_dir(repo)
    worst = 0
    for tier in tiers:
        if tier not in _INTERVALS:
            continue
        if store_hygiene._box_busy():
            _backdate(common_dir, tier)
            continue
        if tier == "daily":
            rc, deferred = store_hygiene._run_daily_outcome(repo)
        else:
            result = run_tier(repo, "weekly")
            rc, deferred = result.rc, result.deferred
        if deferred or rc != 0:
            _backdate(common_dir, tier)
        worst = max(worst, rc)
    return worst


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[0] != "--repo" or argv[2] != "--tiers":
        print("usage: python maintenance_tick.py --repo PATH --tiers daily,weekly", file=sys.stderr)
        return 2
    return run_due(Path(argv[1]), [t for t in argv[3].split(",") if t])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
