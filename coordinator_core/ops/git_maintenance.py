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
  daily   -> store_hygiene.sweep_orphan_pack_files        0 spawns
             then MIDX heal (unlink a MIDX naming a gone  0 spawns
             pack)
             then ONE batch leg: consolidate_pack_batch   ~190 ms (40 packs)
             (while small packs exceed the ceiling) or
             the bounded loose leg (while the loose       ~360 ms (500 objs)
             estimate exceeds STALE_LOOSE_SLACK)
             then commit-graph, only on a call that ran   ~110 ms
             no batch leg
             then git multi-pack-index write              ~70  ms
  weekly  -> git prune --expire=2.weeks.ago              175.0 ms,  1.0 proc
             then git maintenance run --task=pack-refs    53.1 ms,  2.0 procs
             then sweep_orphan_packs()                    ~2 s (one window)

ONLY WEEKLY DEFERS ON THE INDEX. Weekly's `git prune` opens `.git/index`, and
under a held index fails rc 128 (`fatal: .git/index: index file open failed:
Permission denied`); it defers on a live `index.lock`, an in-progress
rebase/merge/bisect/cherry-pick/revert, or unmerged entries. Hourly and daily
run only pack-level legs (`pack-objects`, `multi-pack-index`, `maintenance
--task=commit-graph`) that never open the index, measured to pass with the lock
file present and `.git/index` held open, so they run under `object_store_lock`
alone and never wait on a peer's commit. With ~50 peers committing, an index
gate on daily meant daily almost never ran.

THE DAILY LEG LIST AND ITS PER-CALL CAP are the C0 spike's pins
(docs/research/git-maintenance/2026-11-14-index-free-object-store-legs/
synthesis.md). `git repack -d -q` (it reads the index through
`--indexed-objects`: rc 128 held, 7.4 s free on 42k loose),
`git repack --geometric` (1.3 s on 250 packs, no batch knob) and
`--task=incremental-repack` (adds a pack, dearer than `multi-pack-index write`
for the same MIDX) are not daily legs. One call does at most one batch leg
(together they exceed 500 ms); the rest of the owed work waits for the next
call, and the result reports it as `owed`, never as a deferral.
THE LOOSE LEG is index-free: `rev-list --objects --unpacked --all --reflog`
into `pack-objects`, bounded to `_LOOSE_BATCH_OBJECTS` objects and
`_LOOSE_BATCH_BYTES` of loose file bytes, then the packed loose files are
unlinked. Only objects reachable from refs and reflogs are packed, so an
unreachable loose object stays loose for the weekly `git prune`.
THE MIDX HEAL: a MIDX naming a deleted pack makes every git leg that loads it
fail (`could not load pack 0`), `multi-pack-index write` included; it is
unlinked first and the final leg rewrites it over the packs present.

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
Unreachable objects are never packed by daily (the loose leg lists only objects
reachable from refs and reflogs), so the R9 note above holds and the weekly
prune still reaps them.

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

GIT'S OWN `maintenance.lock` IS STILL NOT GATED ON; ENGINE WRITERS SHARE
`object_store_lock` INSTEAD. An earlier version of this module polled
`<common>/objects/maintenance.lock` (and `<common>/maintenance.lock`) before
running. Measured against git 2.55.0.windows.5 (n=1 machine), holding a file
at either path does not stop `git maintenance run` (`--task=commit-graph` or
`--schedule=weekly`), and 8 concurrent `git maintenance run
--schedule=weekly` invocations against one repo all exited 0: git's file does
not exclude, so the gate was deleted and stays deleted.

That measurement left no mutual exclusion between ENGINE writers, and on
2026-11-14 at 00:54:56 an unattended writer (pid 26264) refreshed every pack's
mtime in this repo's object store and left an unfinished `.tmp-26264-pack-*`
body with no `.idx`; 426 in-pack objects and 182 reachable ones (HEAD's tree
among them) went missing. A repack reading a pack while a fold deletes it, or
two repacks racing, is the shape that loses objects. So `run_tier` (every
tier, every leg) and `sweep_orphan_packs` now run under
`object_store_lock.hold(common_dir, wait_s=0)`, the one kernel-released lock
every engine pack writer shares (`store_hygiene.consolidate_pack_batch`,
`git_objects._write_pack`/`_flush_pack`). It tries ONCE: a held lock means
another engine writer is mid-mutation, so the tier DEFERS and reports (the
`index.lock` contract) and the next ceremony retries; it never queues. The
lock does not gate a user's or third party's own `git gc`.

THE WEEKLY TIER DEFERS AND REPORTS — does not run, does not fail — on a live
`.git/index.lock`, an in-progress rebase/merge/bisect/cherry-pick/revert, or
unmerged index entries (`defer_reason`). H21's exclusive-handle failure is real
here: `git prune` under a held index fails rc 128. Hourly and daily never open
the index and do not consult the predicate.

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
  - Does NOT run the weekly tier when the index is locked or the worktree is
    mid-operation — it defers, and says so. Hourly and daily never read the
    index and run regardless.
  - Does NOT use `git repack` in any form on the daily tier.
  - Does NOT have a cadence of its own. It runs when a ceremony calls it.
"""

GENERATES = []

import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from coordinator_core.git import fleet_maintenance_lock, pack_deletion_pause, store_hygiene
from coordinator_core.git.fleet_maintenance_lock import FleetLockHomeUnresolved, FleetMaintenanceBusy
from coordinator_core.git.object_store_lock import ObjectStoreBusy, hold
from coordinator_core.git.repo_root import absolute_git_dir, git_common_dir
from coordinator_core.git.run import run_git, run_git_stream
from coordinator_core.ipc import register_op
from coordinator_core.ops.reap_stale_locks import _env_float, _env_int, _file_size, _mtime_epoch

_PREFIX = "git-maintenance"

_PRUNE_EXPIRE = "2.weeks.ago"

_ORPHAN_PACK_AGE_SEC = 600
_ORPHAN_PACK_STABILITY_SEC = 2.0

_TIER_ARGV = {
    "hourly": ("maintenance", "run", "--task=commit-graph"),
    "daily": ("maintenance", "run", "--task=commit-graph"),
    "weekly": ("maintenance", "run", "--task=pack-refs"),
}

# Tiers whose legs open `.git/index`; only these consult `defer_reason`.
_INDEX_READING_TIERS = frozenset({"weekly"})

# Was a second literal declaration of
# `_TIER_ARGV`'s key set, policed only by a sync test. `_TIER_ARGV` is the
# actual source of truth; this is derived, not restated.
TIERS = tuple(_TIER_ARGV)

# Daily's last leg: the MIDX over every pack present. Exits 255 on a store with no packs.
_MIDX_WRITE_ARGV = ("multi-pack-index", "write")

# One daily call packs at most this many loose objects / loose-file bytes: 500
# objects measured 359 ms process time, 1000 sits at the 500 ms edge.
_LOOSE_BATCH_OBJECTS = 500
_LOOSE_BATCH_BYTES = 8 * 1024 * 1024


#: `deferred` text while `pack_deletion_pause` holds: nothing unlinked, no git spawned.
PAUSED_REASON = "pack deletion is paused (pack_deletion_pause.PACK_DELETION_PAUSED)"


@dataclass
class MaintenanceResult:

    tier: Optional[str]
    ran: bool = False
    deferred: Optional[str] = None
    pruned: bool = False
    orphan_packs_reaped: int = 0
    orphan_packs_skipped: int = 0
    pack_files_reaped: int = 0
    midx_healed: bool = False
    packs_merged: int = 0
    loose_packed: int = 0
    owed: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def rc(self) -> int:
        return 1 if self.errors else 0


@dataclass(frozen=True)
class OrphanPackSweep:

    reaped: List[Path]
    failed: List[Path]
    skipped: int
    deferred: Optional[str] = None


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

    Holds the object-store lock (try once) around the whole sweep; a held
    lock returns an empty sweep with `deferred` set and unlinks nothing.
    Raises ValueError when an existing `pack_dir` is not shaped `<common>/objects/pack`
    (the lock key is derived from that shape).
    """
    if pack_deletion_pause.paused():
        return OrphanPackSweep(reaped=[], failed=[], skipped=0, deferred=PAUSED_REASON)
    if not pack_dir.is_dir():
        return OrphanPackSweep(reaped=[], failed=[], skipped=0)
    if pack_dir.name != "pack" or pack_dir.parent.name != "objects":
        raise ValueError(f"pack_dir must be <common>/objects/pack, got {pack_dir}")
    try:
        with fleet_maintenance_lock.hold(wait_s=0, holder_label="git-maintenance:orphan-sweep"):
            with hold(pack_dir.parent.parent, wait_s=0, holder_label="git-maintenance:orphan-sweep"):
                return _sweep_orphan_packs_locked(pack_dir, on_wait=on_wait)
    except (ObjectStoreBusy, FleetMaintenanceBusy, FleetLockHomeUnresolved) as exc:
        return OrphanPackSweep(reaped=[], failed=[], skipped=0, deferred=str(exc))


def _sweep_orphan_packs_locked(
    pack_dir: Path,
    *,
    on_wait: Optional[Callable[[], None]] = None,
) -> OrphanPackSweep:
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
    # A clean, in-progress
    # `cherry-pick --no-commit`/`revert --no-commit` leaves CHERRY_PICK_HEAD/
    # REVERT_HEAD present with a clean index and no unmerged entries, which
    # was invisible to every check here even though it is the same class of
    # mid-operation state the other markers exist to catch.
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
        # unanswered git is an obstruction, not a clearance. Reading a timeout
        # as "no unmerged entries" would let maintenance run against an index
        # whose state nobody established.
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

    if pack_deletion_pause.paused():
        result.deferred = PAUSED_REASON
        return result

    raw_git_dir = absolute_git_dir(str(repo))
    if not raw_git_dir:
        result.errors.append("not a git repository")
        return result
    git_dir = Path(raw_git_dir)
    common_raw = git_common_dir(str(repo))
    common = Path(common_raw) if common_raw else git_dir
    if not common.is_absolute():
        common = (repo / common).resolve()

    try:
        with fleet_maintenance_lock.hold(wait_s=0, holder_label=f"git-maintenance:{tier}"):
            with hold(common, wait_s=0, holder_label=f"git-maintenance:{tier}"):
                return _run_tier_locked(repo, tier, result, git_dir, common)
    except (FleetMaintenanceBusy, FleetLockHomeUnresolved) as exc:
        result.deferred = f"fleet maintenance lock is held or unavailable -- another engine is mutating an object store ({exc})"
        return result
    except ObjectStoreBusy as exc:
        result.deferred = f"object-store lock is held -- another engine pack writer is mid-mutation ({exc})"
        return result


def _run_tier_locked(
    repo: Path, tier: str, result: MaintenanceResult, git_dir: Path, common: Path
) -> MaintenanceResult:
    if tier in _INDEX_READING_TIERS:
        reason = defer_reason(repo, git_dir)
        if reason is not None:
            result.deferred = reason
            return result

    # PRUNE RUNS BEFORE THE MAINTENANCE RUN, NOT AFTER. It is no longer
    # load-bearing against daily -- daily's `loose-objects` task is gone
    # (R9 / P153-C25: docs/research/spike-verdicts/2026-09-22-maintenance-
    # reaper-after-daily-pack.md), so nothing upstream of weekly packs an
    # unreachable object out from under `git prune` (LOOSE objects only) any
    # more. It is kept prune-first anyway, harmlessly, because `git gc` is
    # still the alternative it is never worth reaching for -- kill-bar item
    # here (10,068ms/9 procs against prune's 40.6ms/1 proc) -- and because
    # weekly's own `--task=pack-refs` packs nothing, so this order costs
    # nothing to keep.
    #
    # THE TRAP THIS ORDER USED TO GUARD AGAINST, for the record: on a day
    # both tiers fired, `loose-objects` packed unreachable loose objects
    # (including ones young enough that a run last week did not yet catch
    # them) before a prune sequenced after it could see them loose, so that
    # prune reaped nothing and exited 0 -- unreachable history accumulating
    # forever behind a green tier
    # (state/bug-backlog/2026-08-30-the-daily-tier-packs-unreachable-objects-
    # 309a82437447.yaml). The R9 spike also confirmed repacking the pack
    # afterwards (`--cruft-expiration` on weekly) cannot recover this once an
    # object is packed -- packing itself resets the age signal a later reap
    # would need -- so the fix is daily never packing it, not a different
    # reap strategy downstream.
    if tier == "weekly":
        prune = run_git(["prune", f"--expire={_PRUNE_EXPIRE}"], cwd=str(repo))
        if prune.returncode != 0:
            result.errors.append(
                f"prune --expire={_PRUNE_EXPIRE} failed rc={prune.returncode}: {prune.stderr.strip()}"
            )
        else:
            result.pruned = True

    legs: List[Tuple[str, ...]] = [_TIER_ARGV[tier]]
    if tier == "daily":
        batch_ran = _daily_prelude(repo, common, result)
        if result.errors:
            return result
        legs = [] if batch_ran else legs
        if store_hygiene.status(common).pack_count:
            legs.append(_MIDX_WRITE_ARGV)

    for argv in legs:
        proc = run_git(list(argv), cwd=str(repo))
        if proc.returncode != 0:
            result.errors.append(f"{' '.join(argv)} failed rc={proc.returncode}: {proc.stderr.strip()}")
            return result
    result.ran = True
    if tier == "daily":
        result.owed = store_hygiene.owed_by_threshold(common)
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


def _daily_prelude(repo: Path, common: Path, result: MaintenanceResult) -> bool:
    """Daily's legs before the commit-graph and MIDX writes; True when a batch leg ran.

    Reaps orphan pack-dir files, heals a MIDX naming a gone pack, then runs AT MOST ONE
    batch (small-pack consolidation, else the loose leg while the loose estimate is over
    STALE_LOOSE_SLACK): two together exceed the 500 ms bar. A failure lands in
    `result.errors`. The caller holds the object-store lock."""
    result.pack_files_reaped = store_hygiene.sweep_orphan_pack_files(common)
    if store_hygiene.midx_names_missing_pack(common):
        midx = common / "objects" / "pack" / "multi-pack-index"
        if not store_hygiene._unlink_quiet(midx):
            result.errors.append(f"stale multi-pack-index not removable: {midx}")
            return False
        result.midx_healed = True
    result.packs_merged = store_hygiene.consolidate_pack_batch(common)
    if result.packs_merged:
        return True
    if store_hygiene.status(common).loose_estimate > store_hygiene.STALE_LOOSE_SLACK:
        result.loose_packed, error = _pack_loose_batch(repo, common)
        if error:
            result.errors.append(error)
        return result.loose_packed > 0
    return False


def _unlink_loose(path: Path) -> None:
    """Remove one loose object file; git writes them read-only, which Windows refuses to
    unlink, so a refusal retries once after clearing the bit. A file already gone or still
    held open is left for the next call."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        try:
            os.chmod(path, stat.S_IWRITE)
            path.unlink()
        except OSError:
            pass


def _pack_loose_batch(repo: Path, common: Path) -> Tuple[int, Optional[str]]:
    """Pack one bounded batch of reachable loose objects without reading the index.

    `rev-list --objects --unpacked --all --reflog` lists loose objects reachable from refs
    and reflogs (the index is not a source, unlike `git repack -d`); the first
    `_LOOSE_BATCH_OBJECTS` that exist in this store and fit `_LOOSE_BATCH_BYTES` go into one
    `pack-objects` pack, and only after its `.idx` exists are their loose files unlinked.
    Returns `(objects packed, error)`."""
    objects = common / "objects"
    shas: List[str] = []
    total = 0
    with run_git_stream(
        ["rev-list", "--objects", "--unpacked", "--all", "--reflog", "--no-object-names"],
        cwd=str(repo),
        text=True,
    ) as proc:
        for line in proc.stdout:
            sha = line.strip()
            if len(sha) < 40:
                continue
            try:
                size = (objects / sha[:2] / sha[2:]).stat().st_size
            except OSError:
                continue
            if shas and total + size > _LOOSE_BATCH_BYTES:
                break
            shas.append(sha)
            total += size
            if len(shas) >= _LOOSE_BATCH_OBJECTS:
                break
        else:
            if proc.wait() != 0:
                return 0, f"rev-list --unpacked failed rc={proc.returncode}"
    if not shas:
        return 0, None
    packed = run_git(
        ["pack-objects", "-q", str(objects / "pack" / "pack")],
        cwd=str(repo),
        input="".join(sha + "\n" for sha in shas).encode("ascii"),
    )
    name = packed.stdout.strip()
    if packed.returncode != 0 or not name or not (objects / "pack" / f"pack-{name}.idx").exists():
        return 0, f"pack-objects (loose batch) failed rc={packed.returncode}: {packed.stderr.strip()}"
    for sha in shas:
        _unlink_loose(objects / sha[:2] / sha[2:])
    return len(shas), None


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
    if result.tier == "daily":
        line += (
            f"; pack files reaped={result.pack_files_reaped}; midx healed={result.midx_healed}"
            f"; packs merged={result.packs_merged}; loose packed={result.loose_packed}"
            f"; owed={result.owed}"
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
        "pack_files_reaped": result.pack_files_reaped,
        "midx_healed": result.midx_healed,
        "packs_merged": result.packs_merged,
        "loose_packed": result.loose_packed,
        "owed": result.owed,
        "errors": result.errors,
    }
