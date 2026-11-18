"""Timed trigger for the lock-holding git maintenance, hosted on the warm engine's idle tick.

With `gc.auto=0` and `maintenance.auto=false` the engine's maintenance is the only repacker, so
it must run without a human trigger. `on_idle_tick` asks one question per served repo -- "is the
daily (24h) or weekly (7d) stamp stale, or has the store crossed a pack/loose threshold
(`store_hygiene.owed_by_threshold`, stat calls only)?" -- and launches ONE detached child for
the due tiers. A threshold crossing makes daily due even with a fresh stamp; each launch does
one bounded batch, so the store converges over ticks. The child runs the tiers in order, each
holding `object_store_lock` (store_hygiene's daily body, which owns its own load decision, and
`git_maintenance.run_tier("weekly")` after a busy check). Candidates are the served repos plus
every store the engine's pack reader opened (`git_objects.read_store_registry_dir`).

Invariants:
  - The tick thread spawns nothing and samples no load when nothing is owed; the weekly busy check
    (`store_hygiene._box_busy`, which may sleep a sampling gap) and all real work run in the child.
  - Daily is never gated on load here; while `pack_deletion_pause` holds, only daily is offered and
    only when `store_hygiene.midx_upkeep_owed`.
  - The stamp is claimed (touched) at launch, so a slow child is not relaunched. A child that finds
    the box busy (weekly), the lock held or the run failed backdates the stamp to retry after `RETRY_S`, not a whole interval.
  - At most one child launch per tick; repos are staggered across ticks.
  - Stamps live under the repo's git common dir.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.git import pack_deletion_pause
from coordinator_core.git.git_dir import resolve_git_common_dir

#: Seconds between runs of each tier.
DAILY_INTERVAL_S = 24 * 3600
WEEKLY_INTERVAL_S = 7 * 24 * 3600
#: Retry delay after a child skipped a tier (box busy, lock held).
RETRY_S = 3600
#: Minimum age of the daily stamp before a threshold crossing alone relaunches daily.
THRESHOLD_COOLDOWN_S = 900
#: In-process gate: a repo's stamps are re-stat'ed at most this often.
CHECK_INTERVAL_S = 300.0
#: Most registry entries kept; the oldest-mtime ones past it are unlinked.
READ_STORE_REGISTRY_MAX = 256

TIERS = ("daily", "weekly")
_INTERVALS = {"daily": DAILY_INTERVAL_S, "weekly": WEEKLY_INTERVAL_S}
_STAMP_DIR = "coordinator"

_log = logging.getLogger(__name__)
_next_check: dict[Path, float] = {}
_next_paused_check: dict[Path, float] = {}
_registry_next = float("-inf")
_registry_cache: list[Path] = []


def stamp_path(common_dir: Path, tier: str) -> Path:
    """The last-run stamp of `tier` under `common_dir`; its mtime is the claim/run time."""
    return Path(common_dir) / _STAMP_DIR / f"maintenance-tick-{tier}.stamp"


def _owed(common_dir: Path, tier: str, now: float) -> bool:
    """True when `tier`'s stamp is older than its interval. A stamp dated in the future (clock skew,
    a hand touch) is not a claim: it reads as owed, else it would suppress the tier until the clock
    caught up."""
    try:
        age = now - stamp_path(common_dir, tier).stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return age < 0 or age >= _INTERVALS[tier]


def _threshold_cooled(common_dir: Path, now: float) -> bool:
    """True when the daily stamp is at least THRESHOLD_COOLDOWN_S old, or dated in the future. A
    crossing that daily cannot clear (unreachable loose objects over the slack until the weekly
    prune) would otherwise relaunch a child every CHECK_INTERVAL_S."""
    try:
        age = now - stamp_path(common_dir, "daily").stat().st_mtime
    except OSError:
        return True
    return age < 0 or age >= THRESHOLD_COOLDOWN_S


def _owed_by_threshold(common_dir: Path) -> bool:
    """Threshold crossing from stat calls alone; an unreadable store is not owed."""
    from coordinator_core.git import store_hygiene

    try:
        return bool(store_hygiene.owed_by_threshold(common_dir))
    except Exception as exc:  # noqa: BLE001 -- a probe failure must not mask the stamp trigger
        _log.warning("git maintenance threshold probe failed for %s: %s", common_dir, exc)
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


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _registry_repo(common_dir: Path) -> Optional[Path]:
    """The non-bare repo whose `.git` directory is `common_dir`; None for a bare, gone or
    worktree-linked store."""
    if common_dir.name != ".git" or not common_dir.is_dir():
        return None
    return common_dir.parent


def _read_store_repos() -> list[Path]:
    """Repos whose store the engine's pack reader opened (one listdir of the registry). An entry
    naming no resolvable repo is unlinked; an unresolvable settings home logs and yields none."""
    from coordinator_core.git import git_objects

    try:
        registry = git_objects.read_store_registry_dir()
        names = os.listdir(registry)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 -- the registry never fails the tick
        _log.warning("engine read-store registry unreadable (settings_home unresolved?): %s", exc)
        return []
    kept: list[tuple[float, Path, Path]] = []
    temp_root = _temp_root()
    for name in names:
        entry = registry / name
        try:
            line = entry.read_text(encoding="utf-8").strip()
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        repo = _registry_repo(Path(line)) if line else None
        if repo is None or _is_under(repo, temp_root):
            _unlink_quiet(entry)
            continue
        kept.append((mtime, entry, repo))
    kept.sort(key=lambda item: item[0], reverse=True)
    for _, entry, _repo in kept[READ_STORE_REGISTRY_MAX:]:
        _unlink_quiet(entry)
    return [repo for _, _, repo in kept[:READ_STORE_REGISTRY_MAX]]


def _temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _registered_repos(now_mono: float) -> list[Path]:
    """`_read_store_repos`, re-read at most every CHECK_INTERVAL_S."""
    global _registry_next, _registry_cache
    if now_mono >= _registry_next:
        _registry_next = now_mono + CHECK_INTERVAL_S
        _registry_cache = _read_store_repos()
    return _registry_cache


def _due_tiers(common_dir: Path, now: float, paused: bool) -> list[str]:
    """Tiers to launch. Paused: daily alone, and only when the MIDX is owed a rewrite (stat only)."""
    if paused:
        from coordinator_core.git import store_hygiene

        if _threshold_cooled(common_dir, now) and store_hygiene.midx_upkeep_owed(common_dir):
            return ["daily"]
        return []
    due = [t for t in TIERS if _owed(common_dir, t, now)]
    if "daily" not in due and _threshold_cooled(common_dir, now) and _owed_by_threshold(common_dir):
        due.insert(0, "daily")
    return due


def on_idle_tick(repos: Iterable[Path]) -> bool:
    """Launch the due tiers for at most one repo; True when a child was launched.

    Candidates are the served repos plus every store the pack reader opened (the registry). A
    not-owed tick is a few stats and spawns nothing; a paused one (`pack_deletion_pause`) stats
    only for MIDX upkeep and offers daily alone."""
    paused = pack_deletion_pause.paused()
    gate = _next_paused_check if paused else _next_check
    now_mono = time.monotonic()
    now = time.time()
    seen: set[Path] = set()
    for raw in (*repos, *_registered_repos(now_mono)):
        try:
            repo = Path(raw).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if repo in seen:
            continue
        seen.add(repo)
        if now_mono < gate.get(repo, float("-inf")):
            continue
        gate[repo] = now_mono + CHECK_INTERVAL_S
        try:
            dot_git = repo / ".git"
            if not dot_git.exists():
                continue
            common_dir = dot_git if dot_git.is_dir() else resolve_git_common_dir(repo)
            due = _due_tiers(common_dir, now, paused)
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
    from coordinator_core.git import fleet_maintenance_lock
    from coordinator_core.git import store_hygiene
    from coordinator_core.ops.git_maintenance import run_tier

    common_dir = resolve_git_common_dir(repo)
    try:
        with fleet_maintenance_lock.hold(wait_s=0, holder_label="maintenance_tick:run_due"):
            return _run_due_locked(repo, common_dir, tiers, store_hygiene, run_tier)
    except (fleet_maintenance_lock.FleetMaintenanceBusy, fleet_maintenance_lock.FleetLockHomeUnresolved):
        for tier in tiers:
            if tier in _INTERVALS:
                _backdate(common_dir, tier)
        return 0


def _run_due_locked(repo: Path, common_dir: Path, tiers: Iterable[str], store_hygiene, run_tier) -> int:
    worst = 0
    for tier in tiers:
        if tier not in _INTERVALS:
            continue
        if tier == "daily":
            rc, deferred = store_hygiene._run_daily_outcome(repo)
        else:
            if store_hygiene._box_busy():
                _backdate(common_dir, tier)
                continue
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
