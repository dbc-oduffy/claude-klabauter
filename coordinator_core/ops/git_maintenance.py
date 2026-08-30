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

  hourly  -> git maintenance run --task=commit-graph      31.2 ms,  2.0 procs
  daily   -> git maintenance run --schedule=daily        190.6 ms,  6.0 procs
  weekly  -> git prune --expire=2.weeks.ago               40.6 ms,  1.0 proc
             then git maintenance run --schedule=weekly  178.1 ms,  7.0 procs
             then sweep_orphan_packs()                    ~2 s (one window)

THE WEEKLY ORDER IS LOAD-BEARING — prune FIRST. `--schedule=weekly` includes
`loose-objects`, which packs loose objects, unreachable ones included, and
`git prune` only ever removes LOOSE objects. Prune sequenced after the run
therefore reaps nothing and exits 0. See `run_tier` for the full note.

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

EXIT 0 AND REPORT when `<objects>/maintenance.lock` is held. Roughly twenty
sessions can enter one ceremony at once on this box; git errors rather than
corrupts, so a lost race is a no-op worth REPORTING, not a failure worth
failing.

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

GENERATES = []  # mutates only git's own object store via git's own maintenance -- no tracked repo artifact

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from coordinator_core.git.repo_root import absolute_git_dir, git_common_dir
from coordinator_core.ipc import register_op
from coordinator_core.ops.reap_stale_locks import _env_float, _env_int, _file_size, _mtime_epoch
from coordinator_core.win_portability import no_console_creationflags

_PREFIX = "git-maintenance"

TIERS = ("hourly", "daily", "weekly")

_PRUNE_EXPIRE = "2.weeks.ago"

_ORPHAN_PACK_AGE_SEC = 600
_ORPHAN_PACK_STABILITY_SEC = 2.0

# Tier -> the `git maintenance run` argument vector for that tier. Hourly is a
# task list precisely so `prefetch` cannot enter it; daily and weekly are
# schedules because their task sets are git's to define and the prefetch key
# suppresses the one task that would put them over the bar.
_TIER_ARGV = {
    "hourly": ("maintenance", "run", "--task=commit-graph"),
    "daily": ("maintenance", "run", "--schedule=daily"),
    "weekly": ("maintenance", "run", "--schedule=weekly"),
}


@dataclass
class MaintenanceResult:
    """One tier invocation's outcome. `deferred` and `lock_held` are both
    exit-0 outcomes that did no work — a caller reading only the exit code
    cannot tell them from a successful run, which is why they are reported."""

    tier: str
    ran: bool = False
    deferred: Optional[str] = None
    lock_held: bool = False
    pruned: bool = False
    orphan_packs_reaped: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def rc(self) -> int:
        return 1 if self.errors else 0


@dataclass(frozen=True)
class OrphanPackSweep:
    """What one sweep did. `reaped` and `failed` are pack-body paths; `skipped`
    counts candidates that failed a gate, so a caller can distinguish "nothing
    was there" from "everything present was still in flight"."""

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
    age_floor: Optional[int] = None,
    stability_sec: Optional[float] = None,
    no_sleep: bool = False,
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
    if age_floor is None:
        age_floor = _env_int("COORDINATOR_ORPHAN_PACK_REAP_AGE_SEC", _ORPHAN_PACK_AGE_SEC)
    if stability_sec is None:
        stability_sec = _env_float(
            "COORDINATOR_ORPHAN_PACK_REAP_STABILITY_SEC", _ORPHAN_PACK_STABILITY_SEC
        )

    candidates = _orphan_pack_candidates(pack_dir)
    if not candidates:
        return OrphanPackSweep(reaped=[], failed=[], skipped=0)

    now = int(time.time())
    # Gate 2 BEFORE the window, so a set of fresh candidates costs no wait.
    aged = [p for p in candidates if now - _mtime_epoch(p) >= age_floor]
    skipped = len(candidates) - len(aged)
    if not aged:
        return OrphanPackSweep(reaped=[], failed=[], skipped=skipped)

    first = {p: (_mtime_epoch(p), _file_size(p)) for p in aged}

    # THE ONE WINDOW. Not inside the loop below, and not inside a per-file
    # helper -- see this docstring.
    if on_wait is not None:
        on_wait()
    elif not no_sleep:
        time.sleep(stability_sec)

    reaped: List[Path] = []
    failed: List[Path] = []
    for p in aged:
        if not p.exists():
            skipped += 1
            continue
        if (_mtime_epoch(p), _file_size(p)) != first[p]:
            # Gate 3 failed: something is still writing this body.
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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def defer_reason(repo: Path, git_dir: Path) -> Optional[str]:
    """Why this worktree must not be maintained right now, or None.

    Checked in cost order: three `Path.exists()` calls and one `index.lock`
    check cost nothing, so the single spawn (`ls-files --unmerged`) runs only
    when the free checks all pass.

    NO `os.name` BRANCH. Every check here is a plain path test or plain git,
    identical on all three hosts. The failure that motivates the predicate is a
    Windows file-sharing artifact, but a held index and an in-progress rebase
    are equally real on POSIX, and the predicate is exercised against both
    shapes in test.
    """
    if (git_dir / "index.lock").exists():
        return "index.lock is held -- a peer is mid-commit"
    for marker, what in (
        ("REBASE_HEAD", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("BISECT_LOG", "bisect"),
    ):
        if (git_dir / marker).exists():
            return f"{what} in progress ({marker} present)"
    unmerged = _git(repo, "ls-files", "--unmerged")
    if unmerged.returncode == 0 and unmerged.stdout.strip():
        return "index has unmerged entries"
    return None


def run_tier(repo: Path, tier: str) -> MaintenanceResult:
    """Run one maintenance tier against `repo`."""
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

    # THE LOST RACE IS A NO-OP, NOT A FAILURE. Checked before the defer
    # predicate's spawn, so ~20 sessions entering one ceremony pay one
    # `exists()` each rather than one `ls-files` each.
    if (common / "objects" / "maintenance.lock").exists():
        result.lock_held = True
        return result

    reason = defer_reason(repo, git_dir)
    if reason is not None:
        result.deferred = reason
        return result

    # PRUNE RUNS BEFORE THE MAINTENANCE RUN, NOT AFTER, and the order is
    # load-bearing rather than stylistic.
    #
    # `--schedule=weekly` includes the `loose-objects` task, which PACKS loose
    # objects -- unreachable ones included. `git prune` only ever removes LOOSE
    # objects; once garbage has been packed, dropping it needs a full
    # `repack -A -d` or a `gc`, and `gc` is a kill-bar item here (10,068ms/9
    # procs against prune's 40.6ms/1 proc). So a prune sequenced after the
    # maintenance run silently reaps nothing: `loose-objects` has already
    # swept the evidence into a pack, prune exits 0, and unreachable history
    # accumulates forever behind a green tier.
    #
    # This is the same trap git's own docs describe when they contraindicate
    # `gc` beside `loose-objects` -- it bites the prune leg too, and the plan's
    # § Anti-scope names only the `gc` half of it. Found by the plan's
    # falsifier, whose conjunct 4 read FAIL with the legs in the other order.
    if tier == "weekly":
        prune = _git(repo, "prune", f"--expire={_PRUNE_EXPIRE}")
        if prune.returncode != 0:
            result.errors.append(
                f"prune --expire={_PRUNE_EXPIRE} failed rc={prune.returncode}: {prune.stderr.strip()}"
            )
        else:
            result.pruned = True

    proc = _git(repo, *_TIER_ARGV[tier])
    if proc.returncode != 0:
        result.errors.append(f"{' '.join(_TIER_ARGV[tier])} failed rc={proc.returncode}: {proc.stderr.strip()}")
        return result
    result.ran = True
    _stamp(repo)

    if tier != "weekly":
        return result

    # The orphan-pack sweep runs LAST: the maintenance run above is itself a
    # producer of legitimate in-flight `.pack` bodies, and sweeping after it
    # has finished means the age and stability gates are never asked to
    # arbitrate against our own repack.
    swept = sweep_orphan_packs(common / "objects" / "pack")
    result.orphan_packs_reaped = len(swept.reaped)
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
    """One fact, once, plus a terse alternative -- docs/wiki/guard-messaging.md
    § Register."""
    if result.errors:
        for err in result.errors:
            print(f"{_PREFIX}: {err}", file=sys.stderr)
        return
    if result.lock_held:
        print(f"{_PREFIX}: {result.tier} skipped -- maintenance.lock held by a peer", file=sys.stderr)
        return
    if result.deferred:
        print(f"{_PREFIX}: {result.tier} deferred -- {result.deferred}", file=sys.stderr)
        return
    line = f"{_PREFIX}: {result.tier} ran"
    if result.tier == "weekly":
        line += f"; pruned={result.pruned}; orphan packs reaped={result.orphan_packs_reaped}"
    print(line, file=sys.stderr)


def main(argv: Sequence[str]) -> int:
    """`coordinator-git-maintenance <TIER>` — the bin trampoline's entry point.

    No argv parsing beyond the tier: an option surface here would be a second
    way to say what the tier already says.
    """
    args = list(argv)
    if len(args) != 1 or args[0] not in _TIER_ARGV:
        print(f"{_PREFIX}: usage: coordinator-git-maintenance <{'|'.join(TIERS)}>", file=sys.stderr)
        return 2
    result = run_tier(Path.cwd(), args[0])
    _report(result)
    return result.rc


@register_op("git.maintenance")
async def _git_maintenance(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `git.maintenance` handler. `params["tier"]` is required."""
    tier = params.get("tier")
    if tier not in _TIER_ARGV:
        return {
            "ok": False,
            "error": f"unknown tier {tier!r} -- expected one of {', '.join(TIERS)}",
        }
    repo = Path(params.get("repo") or repo_root or Path.cwd())
    result = run_tier(repo, tier)
    return {
        "ok": not result.errors,
        "tier": result.tier,
        "ran": result.ran,
        "deferred": result.deferred,
        "lock_held": result.lock_held,
        "pruned": result.pruned,
        "orphan_packs_reaped": result.orphan_packs_reaped,
        "errors": result.errors,
    }
