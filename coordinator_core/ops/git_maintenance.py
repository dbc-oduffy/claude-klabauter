"""
coordinator_core.ops.git_maintenance — the `git.maintenance` op: run one
maintenance TIER against the caller's worktree, from a coordinator ceremony.

Purpose: replace auto-gc and the OS scheduler as the thing that keeps a
coordinator worktree maintained. `gc.auto=0` (configure_git `_SETTINGS`) turns
auto-gc off; `maintenance.auto=false` (install `git_perf_config.apply`) turns
git's own opportunistic maintenance off; `git maintenance start` is ruled out
because it writes a schtasks/launchctl/systemd entry and, on Windows, opens a
console window per repo per hour. What remains is this: a ceremony calls a tier,
the tier does the work, and nothing runs on a timer.

THE OP TAKES A TIER, NEVER A SCHEDULE OR A TASK LIST. The mapping, and the
measured cost of each (spike figures, n=1, lower bounds — see § below):

  hourly  -> git maintenance run --task=commit-graph      40.6 ms,  2.0 procs
  daily   -> git maintenance run --task=commit-graph      ~190   ms (derived,
             --task=incremental-repack                    see below)
  weekly  -> git prune --expire=2.weeks.ago              175.0 ms,  1.0 proc
             then git maintenance run --task=pack-refs    53.1 ms,  2.0 procs
             then sweep_orphan_packs()                    ~2 s (one window)

DAILY DROPPED `loose-objects` (R9 / P153-C24, viable, candidate (a):
docs/research/spike-verdicts/2026-09-22-maintenance-reaper-after-daily-pack.md).
`loose-objects` packs loose objects, unreachable ones included, and once an
unreachable object is packed the weekly tier's plain `git prune` — which only
ever removes LOOSE objects — reaps nothing: filed as
state/bug-backlog/2026-08-30-the-daily-tier-packs-unreachable-objects-309a82437447.yaml.
The spike's kill criteria confirmed candidate (a) reaps the planted blob 5/5
both daily-then-weekly and weekly-alone, and candidate (b) (repack the pack
with `--cruft-expiration` on weekly instead) does not: the cruft pack stamps
a fresh mtime at pack-creation time, so the object never ages out from
inside a pack. Re-ordering does not recover this once the object is
packed — the fix is to stop daily from ever packing it, not to reap it
differently afterwards.
Removing `loose-objects` leaves daily's own measured 392.6 ms figure minus
the 203.1 ms `loose-objects` was measured to cost, ~190 ms — the module's
own decomposition comment, not a fresh spike measurement; the acceptance
guard's own run is the first real data point for this shape.
NAMED RESIDUAL, PER THE SPIKE: `incremental-repack` alone does not touch
loose objects, so dropping `loose-objects` from daily means nothing packs
*reachable* loose objects either; under `gc.auto=0` those accumulate
without bound unless something eventually packs them. Requirement question,
left open rather than silently dropped: does the weekly tier need a
loose-objects-equivalent pass for the reachable set, ordered so it can never
re-introduce this same trap for the unreachable set (never running any
loose-objects/full-repack task ahead of that same run's own prune leg)?

THE WEEKLY TIER WAS `--schedule=weekly` AND IT WENT OVER THE BAR. Measured at
515.6 ms mean / 9 procs (N=8 independent COLD repos, each registered through
`configure_git` + `git_perf_config.apply()` and given 200 commits of churn,
one first-run sample each). Five of eight samples landed at or above 500 ms.
Decomposing it named the cause rather than inviting a shave: commit-graph
(40.6) + loose-objects (203.1) + incremental-repack (31.2) = 328 ms of that
bundle is what `--schedule=daily` had ALREADY run that day, because
`maintenance.strategy=incremental` makes schedules cumulative. The only
weekly-unique work is `pack-refs` and this module's own prune. So the tier
was rebuilt around what it is FOR -- refs compaction and unreachable-object
reaping -- instead of around a schedule keyword that re-does yesterday.
Numbers above are means over N independent COLD registered repos. Do not
restore figures measured against UNREGISTERED repos or a single warm sample --
both under-measure, and both were tried and retracted.

WEEKLY'S OWN PRUNE-BEFORE-PACK-REFS ORDER IS NO LONGER LOAD-BEARING ON ITS
OWN -- `--task=pack-refs` packs nothing. It used to also carry a cross-tier
duty: the daily tier's `loose-objects` task packed loose objects, unreachable
ones included, so a weekly prune sequenced after a daily run that already
fired would reap nothing and exit 0 (filed as
state/bug-backlog/2026-08-30-the-daily-tier-packs-unreachable-objects-309a82437447.yaml).
Daily no longer runs `loose-objects` (R9 / P153-C25, see above), so that
cross-tier trap is closed at the source rather than worked around by
ordering, and prune-before-pack-refs is kept only because it is still
harmless, not because anything still depends on it. See `run_tier` for the
full note.

The ~2 s is a SLEEP, not process time, and the brightline is process time. That
distinction is exactly why the sweep is weekly-tier work and never commit-path
work: wall clock on this box measures peer load, never our cost.

HOURLY MUST NOT BE `--schedule=hourly`. That schedule includes `prefetch`
alongside `commit-graph`, and `prefetch` is a `git fetch` against every remote
plus two `gh auth git-credential` round-trips: 293.8 ms and 11.2 processes on its
own. The whole design here is deliberately network-free and `prefetch` is the
only task that is not.

THE DAILY AND WEEKLY FIGURES HOLD ONLY WITH `maintenance.prefetch.enabled=false`
SET. git's schedules cascade, so `prefetch` runs at daily and weekly too; with it
enabled those tiers measure 575.0 ms and 618.8 ms, both over the 500 ms
brightline. This module does NOT set that key and must not — `git_perf_config`
owns it, per-repo, once at install. This module only measures under the bar once
it is set.

NO `maintenance.lock` GATE HERE, BY MEASUREMENT, NOT OVERSIGHT. An earlier
version of this module polled `<common>/objects/maintenance.lock` (and, per
review, `<common>/maintenance.lock` was the other candidate) before running,
on the theory that `git maintenance run` takes such a lock and a peer holding
it means a lost race worth reporting rather than failing. Direct measurement
against git 2.55.0.windows.5 (n=1 machine; a future git version could differ)
found this false at both candidate paths: holding a file at either location
does not stop `git maintenance run` (`--task=commit-graph` or
`--schedule=weekly`) from running anyway, leaving both files untouched, and 8
concurrent `git maintenance run --schedule=weekly` invocations against one
repo all exited 0 with no lock-related stderr. The gate never fired and the
concurrency it claimed to arbitrate is git's own to arbitrate, not ours — so
the check, the result field, and the report branch were deleted rather than
pointed at a corrected path. No replacement locking mechanism was added: the
measurement says none is needed.

DEFER AND REPORT — do not run, do not fail — on a live `.git/index.lock`, an
in-progress rebase/merge/bisect, or unmerged index entries. A ceremony boundary
is exactly when a peer is most likely to be mid-write. The predicate exists
because H21's exclusive-handle failure is real here: `git prune` under a held
index fails rc 128 with `fatal: .git/index: index file open failed: Permission
denied`. Their spike tested the five maintenance tasks and not the reaper they
proposed; ours tested the reaper, and it is the leg that breaks.

`git prune --expire=`, NEVER `git gc --prune=`. The memo offers them as
alternatives; they are not. Measured here: `git prune --expire=2.weeks.ago` is
40.6 ms and 1 process, `git gc --prune=2.weeks.ago` is 10,068.8 ms and 9
processes — twenty times over the brightline, a kill-bar item on sight. Git's own
docs add an independent reason: enabling `gc` beside `loose-objects` is
contraindicated, because `gc` writes unreachable objects out as loose ones for a
later step and `loose-objects` immediately re-packs them.

WHY THE PRUNE LEG EXISTS AT ALL. Under `maintenance.strategy=incremental`
nothing ever drops an unreachable object: `loose-objects` packs them and
`incremental-repack` repacks them. So `gc.auto=0` without this leg means
unreachable history accumulates forever. The spike planted an unreachable blob
and watched it survive both full schedules and every individual task.

Expiry age: `2.weeks.ago`. It is git's own `gc.pruneExpire` default and nothing
in this design justifies diverging from it — a shorter window would need an
argument about how quickly this worktree's unreachable objects actually
accumulate, and no such measurement exists.

THE SPIKE FIGURES ARE LOWER BOUNDS, NOT TARGETS. They came from a freshly cloned
probe with little churn between runs, so `commit-graph`, `loose-objects` and
`incremental-repack` all had less to do than they will on a worktree in daily
use. A tier measuring materially above them here is expected; only the 500 ms bar
matters. n=1 today — the acceptance guard's first green run is the second data
point, not a confirmation of the first.

Spec backlink: docs/plans/2026-08-30-ceremony-driven-git-maintenance.md § C4, § C5.

Negative-spec:
  - Does NOT call `git maintenance register`. It writes this repo's path into
    the multi-valued `maintenance.repo` key in the operator's GLOBAL config,
    read only by `git for-each-repo`, run only by the scheduler, which this
    design never runs. It buys nothing and costs a machine-wide out-of-repo
    write surface.
  - Does NOT call `git maintenance start`/`stop`. No scheduler, ever.
  - Does NOT set `maintenance.prefetch.enabled`, `maintenance.auto`, or
    `maintenance.strategy`. `git_perf_config.apply()` owns all three.
  - Does NOT use `git gc` in any form as the unreachable-object reaper.
  - Does NOT write to `~/.gitconfig` or any global config.
  - Does NOT run when the index is locked or the worktree is mid-operation —
    it defers, and says so.
  - Does NOT have a cadence of its own. It runs when a ceremony calls it.
"""

GENERATES = []

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from coordinator_core.git.repo_root import absolute_git_dir, git_common_dir
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops.reap_stale_locks import _env_float, _env_int, _file_size, _mtime_epoch

_PREFIX = "git-maintenance"

_PRUNE_EXPIRE = "2.weeks.ago"

_ORPHAN_PACK_AGE_SEC = 600
_ORPHAN_PACK_STABILITY_SEC = 2.0

_TIER_ARGV = {
    "hourly": ("maintenance", "run", "--task=commit-graph"),
    "daily": ("maintenance", "run", "--task=commit-graph", "--task=incremental-repack"),
    "weekly": ("maintenance", "run", "--task=pack-refs"),
}

# `_TIER_ARGV`'s key set, policed only by a sync test. `_TIER_ARGV` is the
TIERS = tuple(_TIER_ARGV)


@dataclass
class MaintenanceResult:

    tier: Optional[str]
    ran: bool = False
    deferred: Optional[str] = None
    pruned: bool = False
    orphan_packs_reaped: int = 0
    orphan_packs_skipped: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def rc(self) -> int:
        return 1 if self.errors else 0


@dataclass(frozen=True)
class OrphanPackSweep:

    reaped: List[Path]
    failed: List[Path]
    skipped: int


def _orphan_pack_candidates(pack_dir: Path) -> List[Path]:
    """Every `tmp-*-pack-*.pack` body in `pack_dir` with no sibling `.idx`.

    Gate 1, applied at collection time so a completed pack never enters the
    stability window at all.

    THE LEADING DOT IS OPTIONAL, deliberately. Git writes these bodies as
    `.tmp-<pid>-pack-<name>.pack` on the versions in use here, and the plan
    text names that shape throughout — but the plan's own falsifier plants the
    dotless `tmp-<...>-pack-<...>.pack`. Rather than pick a winner and have the
    instrument and the implementation disagree about what garbage looks like,
    both are matched: neither shape is a name anything but git's own temp-pack
    machinery produces, so widening here admits no new class of file. A matcher
    that accepted only one would silently leave the other's garbage on disk
    forever, which is the exact failure this reaper exists to prevent.

    Confirmed only against git 2.55.0.windows.5 (n=1 machine) -- git's
    temp-pack naming has changed across releases historically, so this is a
    measured-here claim, not a guarantee across every git version in the
    fleet.
    """
    if not pack_dir.is_dir():
        return []
    out: List[Path] = []
    for entry in pack_dir.iterdir():
        name = entry.name
        stem = name[1:] if name.startswith(".") else name
        if not stem.startswith("tmp-") or "-pack-" not in stem:
            continue
        if not name.endswith(".pack"):
            continue
        if entry.with_suffix(".idx").exists():
            continue
        out.append(entry)
    return out


def sweep_orphan_packs(
    pack_dir: Path,
    *,
    on_wait: Optional[Callable[[], None]] = None,
) -> OrphanPackSweep:
    """Reaper (a): orphan `.tmp-*-pack-*` bodies, paying ONE stability window.

    The pack-garbage class `git gc --prune=now` provably does not reap — the
    spike planted such a file, ran it, watched it exit 0, and found
    `count-objects -v` still reporting `garbage: 1` before and after. Nothing
    in git reaps this class, so `gc.auto=0` removes no reaper here; it removes
    the PRODUCER (racing foreground repacks under `gc.autoDetach=false`, which
    left 1.1 GB of these bodies in this worktree).

    THE THREE GATES, all of which must hold before any unlink:

      1. NO SIBLING `.idx` — necessary, NOT sufficient. It separates an orphan
         from a completed pack, but a legitimate in-flight repack writes its
         `.pack` body first and creates the `.idx` only at the very end, so a
         live repack's output ALSO has no `.idx` for its whole duration. This
         gate alone would race a healthy repack.
      2. AGE — older than `COORDINATOR_ORPHAN_PACK_REAP_AGE_SEC` (default
         600 s). Defended against this plan's own § Problem observation that a
         repack of a 412 MB pack legitimately runs for MINUTES: 600 s is
         roughly ten times that plausible worst case. It is not "reuse the
         maintenance lock floor because it is already there" — the knob is a
         sibling of `COORDINATOR_LOCK_REAP_MAINT_AGE_SEC` in shape and default
         so the two classes can diverge later without inheriting each other's
         tuning.
      3. STABLE — (mtime, size) unchanged across the re-sample window.

    Gates 2 and 3 are what actually separate an orphan from an in-flight
    repack. Gate 1 alone is a coin flip; all three are load-bearing.

    NAMED RESIDUAL SCOPE CUT, not a closed race: the 600s margin in gate 2 is
    sized against a BUSY writer that keeps bumping mtime throughout its run --
    it says nothing about an idle-but-still-open one. A legitimate repack that
    stalls on I/O (disk contention, a throttled/suspended process) for over
    600s without touching the file, and stays stalled through the one
    re-sample window, passes both gates 2 and 3 while the writer still holds
    an open handle. On POSIX the unlink does not corrupt the live inode, but
    it does remove the directory entry the writer's own rename-into-place step
    will target, so the in-flight repack silently fails or produces no visible
    artifact. On Windows the open-handle unlink more likely raises `OSError`
    (caught into `failed`), which fails loud rather than silent, but that is
    platform-dependent behavior, not a designed guarantee. An open-handle
    check before unlinking would close this, but is OS-specific and racy in
    its own right; this is an accepted scope cut, not a race believed already
    closed.

    ONE STABILITY WINDOW PER SWEEP, NEVER ONE PER FILE. The whole candidate set
    is sampled, the window is waited ONCE, then the whole set is re-sampled. At
    the orphan counts observed in this worktree a per-file window would be
    roughly ten serial 2 s waits — which is also why this is weekly-tier work
    and never commit-path work.

    WHY `reap_stale_locks.stale_and_stable` IS NOT CALLED, though it is the
    same gate. Its sample/wait/re-sample cycle is per-CALL and therefore
    per-file: N candidates cost N windows through it, exactly the cost the
    batching above exists to avoid. The age+stability SEMANTICS are reused
    verbatim by importing that module's sampling primitives and env-knob
    readers, so the two reapers cannot drift on what "stale" means; only the
    loop shape differs, and it differs for a measured reason.

    `reap_stale_locks.py` itself is untouched: its Purpose line, its closed
    "Locks covered" list, its byte-for-byte-parity clause, its `GENERATES = []`
    comment, and the rc-0/1/2 contract `lock_preflight` consumes on the commit
    path all stay exactly as they are.

    `on_wait`, when given, replaces the real sleep entirely — the injectable
    re-sample seam, mirroring `stale_and_stable`'s own.
    """
    age_floor = _env_int("COORDINATOR_ORPHAN_PACK_REAP_AGE_SEC", _ORPHAN_PACK_AGE_SEC)
    stability_sec = _env_float(
        "COORDINATOR_ORPHAN_PACK_REAP_STABILITY_SEC", _ORPHAN_PACK_STABILITY_SEC
    )

    candidates = _orphan_pack_candidates(pack_dir)
    if not candidates:
        return OrphanPackSweep(reaped=[], failed=[], skipped=0)

    now = int(time.time())
    aged = [p for p in candidates if now - _mtime_epoch(p) >= age_floor]
    skipped = len(candidates) - len(aged)
    if not aged:
        return OrphanPackSweep(reaped=[], failed=[], skipped=skipped)

    first = {p: (_mtime_epoch(p), _file_size(p)) for p in aged}

    if on_wait is not None:
        on_wait()
    else:
        time.sleep(stability_sec)

    reaped: List[Path] = []
    failed: List[Path] = []
    for p in aged:
        if not p.exists():
            skipped += 1
            continue
        if (_mtime_epoch(p), _file_size(p)) != first[p]:
            skipped += 1
            continue
        try:
            p.unlink()
        except FileNotFoundError:
            skipped += 1
        except OSError:
            failed.append(p)
        else:
            reaped.append(p)

    return OrphanPackSweep(reaped=reaped, failed=failed, skipped=skipped)


def defer_reason(repo: Path, git_dir: Path) -> Optional[str]:
    if (git_dir / "index.lock").exists():
        return "index.lock is held -- a peer is mid-commit"
    # `cherry-pick --no-commit`/`revert --no-commit` leaves CHERRY_PICK_HEAD/
    # REVERT_HEAD present with a clean index and no unmerged entries, which
    for marker, what in (
        ("REBASE_HEAD", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("BISECT_LOG", "bisect"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ):
        if (git_dir / marker).exists():
            return f"{what} in progress ({marker} present)"
    unmerged = run_git(["ls-files", "--unmerged"], cwd=str(repo))
    if unmerged.timed_out or unmerged.returncode == 127:
        # This function gates a DESTRUCTIVE tier (gc/prune/repack), so an
        return "could not read index state (git did not answer)"
    if unmerged.returncode == 0 and unmerged.stdout.strip():
        return "index has unmerged entries"
    return None


def run_tier(repo: Path, tier: Optional[str]) -> MaintenanceResult:
    if tier not in _TIER_ARGV:
        result = MaintenanceResult(tier=tier)
        result.errors.append(f"unknown tier {tier!r} -- expected one of {', '.join(TIERS)}")
        return result

    result = MaintenanceResult(tier=tier)

    raw_git_dir = absolute_git_dir(str(repo))
    if not raw_git_dir:
        result.errors.append("not a git repository")
        return result
    git_dir = Path(raw_git_dir)
    common_raw = git_common_dir(str(repo))
    common = Path(common_raw) if common_raw else git_dir
    if not common.is_absolute():
        common = (repo / common).resolve()

    reason = defer_reason(repo, git_dir)
    if reason is not None:
        result.deferred = reason
        return result

    # PRUNE RUNS BEFORE THE MAINTENANCE RUN, NOT AFTER. It is no longer
    # (R9 / P153-C25: docs/research/spike-verdicts/2026-09-22-maintenance-
    if tier == "weekly":
        prune = run_git(["prune", f"--expire={_PRUNE_EXPIRE}"], cwd=str(repo))
        if prune.returncode != 0:
            result.errors.append(
                f"prune --expire={_PRUNE_EXPIRE} failed rc={prune.returncode}: {prune.stderr.strip()}"
            )
        else:
            result.pruned = True

    proc = run_git(list(_TIER_ARGV[tier]), cwd=str(repo))
    if proc.returncode != 0:
        result.errors.append(f"{' '.join(_TIER_ARGV[tier])} failed rc={proc.returncode}: {proc.stderr.strip()}")
        return result
    result.ran = True
    if not result.errors:
        _stamp(repo)

    if tier != "weekly":
        return result

    swept = sweep_orphan_packs(common / "objects" / "pack")
    result.orphan_packs_reaped = len(swept.reaped)
    result.orphan_packs_skipped = swept.skipped
    for failed in swept.failed:
        result.errors.append(f"orphan pack not removed: {failed}")

    return result


def _stamp(repo: Path) -> None:
    """Record that maintenance ran, best-effort.

    ANY successful tier stamps the one class. Without this the class reads
    NEVER_STAMPED forever and the liveness signal is decoration — and the
    liveness stamp is the ONLY surface on which "maintenance never ran" and
    "maintenance ran and is fine" look different, because nothing here
    self-triggers.

    Import-local and swallowed: a liveness store that cannot be written must
    never turn a successful maintenance run into a failed one.
    """
    try:
        from coordinator_core.ops.ceremony.housekeeping_liveness import (
            GIT_MAINTENANCE,
            stamp_liveness,
        )

        stamp_liveness(str(repo), GIT_MAINTENANCE)
    except Exception:  # noqa: BLE001 -- a liveness stamp never fails a real run
        pass


def _report(result: MaintenanceResult) -> None:
    if result.errors:
        for err in result.errors:
            print(f"{_PREFIX}: {err}", file=sys.stderr)
        return
    if result.deferred:
        print(f"{_PREFIX}: {result.tier} deferred -- {result.deferred}", file=sys.stderr)
        return
    line = f"{_PREFIX}: {result.tier} ran"
    if result.tier == "weekly":
        line += (
            f"; pruned={result.pruned}; orphan packs reaped={result.orphan_packs_reaped}"
            f"; skipped={result.orphan_packs_skipped}"
        )
    print(line, file=sys.stderr)


def main(argv: Sequence[str]) -> int:
    args = list(argv)
    if len(args) != 1 or args[0] not in _TIER_ARGV:
        print(f"{_PREFIX}: usage: coordinator-git-maintenance <{'|'.join(TIERS)}>", file=sys.stderr)
        return 2
    result = run_tier(Path.cwd(), args[0])
    _report(result)
    return result.rc


@register_op("git.maintenance")
def _git_maintenance(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `git.maintenance` handler. `params["tier"]` is required.

    Tier validity was checked a third time
    here; `run_tier` already rejects an unknown tier into `result.errors`,
    which this handler already serialises, so the pre-check bought nothing.
    """
    tier = params.get("tier")
    repo = Path(params.get("repo") or repo_root or Path.cwd())
    result = run_tier(repo, tier)
    return {
        "ok": not result.errors,
        "tier": result.tier,
        "ran": result.ran,
        "deferred": result.deferred,
        "pruned": result.pruned,
        "orphan_packs_reaped": result.orphan_packs_reaped,
        "errors": result.errors,
    }
