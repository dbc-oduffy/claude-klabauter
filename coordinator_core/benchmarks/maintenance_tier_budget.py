"""
coordinator_core.benchmarks.maintenance_tier_budget -- the 500ms-per-tier
brightline oracle for `coordinator_core.ops.git_maintenance`.

Purpose: this used to live inside the plan-lifecycle artifact
`docs/plans/2026-08-30-ceremony-driven-git-maintenance.falsifier.py`, loaded
by `coordinator_core/ops/test_git_maintenance.py` via a hardcoded
`importlib` path into `docs/plans/`. Closed plans are archived to
`archive/specs/YYYY-MM/` and nothing moves a plan's `.falsifier.py` sidecar
alongside its `.md` when that happens -- so archiving that plan would have
silently broken a cadence-tier test for a fact (the per-tier process-time
budget) that has nothing to do with the plan document's own lifecycle. The
oracle now lives here, in `coordinator_core/` proper, and the falsifier
imports it from here -- the dependency runs one way, repo owns the oracle,
the plan artifact borrows it, never the reverse.

`os.chdir` NOTE (checked, not a defect, kept here for whoever changes the
test runner next): `_apply_coordinator_registration` mutates and restores
`os.getcwd()` via try/finally, and every caller here runs it sequentially,
never concurrently, so this is safe under pytest-xdist's process-based `-n`
workers (each worker is a separate process; no two tests share one process's
cwd). This would race under a THREAD-based parallel runner. If this suite
ever moves to one, this function needs a lock or a `cwd=` kwarg on the git
calls instead of a chdir.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from coordinator_core.git.run import run_git

SCRATCH_ROOT = Path(tempfile.gettempdir()) / "coordinator-falsifier"

# How the brightline conjunct amortises. `git maintenance run` is not
# idempotent, so amortisation cannot come from repeats; it comes from N
# independent COLD samples, each against its own freshly-churned repo. See
# check_maintenance_tier_budget's docstring, note (2b).
_TIER_BUDGET_SAMPLES = 3
_TIER_BUDGET_CHURN = 200

# `_churn` and `_make_throwaway_clone` below spawn git only to build each
# sample's FIXTURE (a freshly-churned throwaway repo), timed separately via
# `batched_process_time_ms` -- see `coordinator_core.benchmarks`'s module
# docstring, "Measured-window discipline". Migrated per G7
# (test_shared_git_runner.py).


def _apply_coordinator_registration(repo: Path) -> None:
    """Invoke coordinator's OWN registration path against `repo` -- the two
    writers an install runs, nothing hand-rolled here."""
    from coordinator_core.install import git_perf_config
    from coordinator_core.ops import configure_git

    cwd = os.getcwd()
    try:
        os.chdir(str(repo))
        configure_git.main([])
    finally:
        os.chdir(cwd)
    git_perf_config.apply(repo)


def _churn(repo: Path, commits: int) -> None:
    """Give a repo real work to maintain. A tier measured against a tidy repo
    reproduces the optimistic condition the original spike measured under and
    reads green whatever the code does."""
    for i in range(commits):
        (repo / "churn.txt").write_text("line %d\n" % i, encoding="utf-8")
        run_git(["add", "churn.txt"], cwd=str(repo))
        run_git(
            ["-c", "user.email=f@example.com", "-c", "user.name=f",
             "commit", "-q", "-m", "churn %d" % i],
            cwd=str(repo),
        )


def _make_throwaway_clone() -> Path:
    """A brand-new, freshly-inited git repo under the scratch dir -- stands
    in for one registered coordinator worktree. Never touches the live
    claude-klabauter .git.
    """
    work = Path(tempfile.mkdtemp(prefix="falsifier-worktree-", dir=str(SCRATCH_ROOT)))
    run_git(["init", "-q"], cwd=str(work))
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_git(["add", "seed.txt"], cwd=str(work))
    run_git(
        ["-c", "user.email=falsifier@example.com", "-c", "user.name=falsifier",
         "commit", "-q", "-m", "seed"],
        cwd=str(work),
    )
    return work


def check_maintenance_tier_budget(repo: Path):
    """Every maintenance tier runs under the 500ms process-time brightline.

    Measures the real tier argv vectors the ceremony op ships, against a repo
    carrying INDUCED CHURN -- a tier measured against a repo with nothing to do
    reproduces the same optimistic condition the spike measured under, and
    would read green whatever the code did.

    PROCESS TIME AND SPAWN COUNT, NEVER WALL CLOCK: wall clock on this box
    measures the ~50 peer sessions' load, not our cost.

    <!-- Review: coordinator:code-reviewer (a75785618af408dd8) -- two P1s on
    this conjunct, both applied:

    (1) leg ORDER. `run_tier` runs `git prune --expire=...` BEFORE the
    maintenance-run leg. This was load-bearing when the weekly tier was
    `--schedule=weekly`, which included `loose-objects` -- that task PACKS
    loose objects (unreachable ones included) and `prune` only removes LOOSE
    ones, so a prune sequenced after it reaped nothing and exited 0. The
    weekly tier has since been killed and rebuilt as `--task=pack-refs`,
    which packs nothing, so the order no longer matters WITHIN the tier. It
    is still built from `run_tier`'s own order rather than restated, because
    the daily tier does still run `loose-objects` and defeats the prune the
    same way across tiers (state/bug-backlog/2026-08-30-the-daily-tier-packs-
    unreachable-objects-309a82437447.yaml). The
    legs below are now built directly from `gm._TIER_ARGV`/`gm._PRUNE_EXPIRE`
    in `run_tier`'s own prune-then-maintenance-run order for "weekly", rather
    than restating a hand-picked order that had drifted from it.

    (2) k=3 AVERAGING OF A NON-IDEMPOTENT COMMAND. `git maintenance run` (and
    `git prune`) do real work on their first invocation against churned state
    and near-nothing on repeats once the repo is already tidy --
    `batched_process_time_ms`'s k-repeat-and-divide model assumes uniform
    per-invocation cost, which averaging 1 real run with k-1 cheap no-op
    reruns violates, systematically UNDERSTATING true first-run cost. Fixed
    by measuring each leg with k=1: every argv below runs exactly once against
    this repo's current state, so there is no repeat to be a no-op against --
    the sample IS the first (and only) invocation. This trades the k=20
    default's sub-tick averaging resolution (~0.78ms) for Windows tick
    quantisation (~15.6ms) on a single sample; accepted because honesty about
    non-idempotent first-run cost matters more here than sub-tick precision,
    and margin to the 500ms bar was measured at 31-218ms in the prior spike --
    comfortably clear of one tick's slop. `sample["rc"]` is also now asserted
    per leg (separate finding): a leg whose command errored immediately would
    otherwise read as a cheap PASS with no correctness check. -->

    <!-- Second measurement pass, after the k=1 fix above proved necessary but
    not sufficient. Two further defects, both applied:

    (2a) THE TIERS SHARED ONE REPO, IN ORDER. hourly, then daily, then weekly,
    against the same worktree -- so the weekly leg was measured against a repo
    the two cheaper tiers had already partly tidied. That is the same
    non-idempotence error as (2) above, displaced from repeats onto tiers, and
    it understates in the same direction. Each tier now provisions its own
    freshly-churned repo per sample, which is also why this conjunct ignores
    the `repo` it is handed: by the time it runs, that worktree has been
    registered and had garbage planted in it by the sibling conjuncts.

    (2b) ONE SAMPLE IS NOT A MEASUREMENT ON WINDOWS. k=1 trades repeat-averaging
    away for ~15.6ms tick quantisation, and a single quantised sample is noise
    -- an independent k=1 run recorded a full `--schedule=weekly` maintenance
    run at 0.0ms, i.e. zero ticks. Amortisation has to come from somewhere;
    with repeats ruled out by non-idempotence it comes from N INDEPENDENT COLD
    samples, each against its own fresh repo, averaged. The mean over N is what
    is compared to the bar; the max is reported beside it so a fat tail is
    visible rather than averaged away. N=3 keeps this instrument runnable in a
    few minutes; raise `_TIER_BUDGET_SAMPLES` for a higher-N run rather than
    standing up a second file -- a retired standalone probe at N=12 once read
    hourly=41.7ms, daily=16.9ms, weekly=222.7ms (max 265.6ms), all under the
    bar, but it measured `--schedule=weekly`, a composition this module no
    longer ships, so that reading is retired, not authoritative. -->
    """
    try:
        from coordinator_core.benchmarks.process_time import batched_process_time_ms
        from coordinator_core.ops import git_maintenance as gm
    except ImportError as exc:
        return False, "maintenance-tier entrypoint not importable: %s" % exc

    def _legs_for(tier, target):
        # Mirrors `run_tier`'s own order: for "weekly", prune runs BEFORE the
        # `--schedule=weekly` maintenance-run leg (see docstring above).
        out = []
        if tier == "weekly":
            out.append(["git", "-C", str(target), "prune", "--expire=%s" % gm._PRUNE_EXPIRE])
        out.append(["git", "-C", str(target), *gm._TIER_ARGV[tier]])
        return out

    worst = 0.0
    breaches = []
    parts = []
    rc_failures = []
    for tier in gm.TIERS:
        totals = []
        proc_counts = []
        for _ in range(_TIER_BUDGET_SAMPLES):
            target = _make_throwaway_clone()
            _apply_coordinator_registration(target)
            _churn(target, _TIER_BUDGET_CHURN)
            total_ms = 0.0
            total_procs = 0.0
            for cmd in _legs_for(tier, target):
                sample = batched_process_time_ms(cmd, k=1, cwd=str(target))
                if sample["rc"] != 0:
                    rc_failures.append("%s: %r rc=%d" % (tier, cmd, sample["rc"]))
                total_ms += sample["process_time_ms"]
                total_procs += sample["procs_per_call"]
            totals.append(total_ms)
            proc_counts.append(total_procs)
        mean_ms = sum(totals) / len(totals)
        parts.append(
            "%s=%.1fms mean of %d cold (max %.1f) /%.1f procs"
            % (tier, mean_ms, len(totals), max(totals), sum(proc_counts) / len(proc_counts))
        )
        worst = max(worst, mean_ms)
        if mean_ms >= 500:
            breaches.append(tier)

    ok = not breaches and not rc_failures
    detail = "%s (bar 500ms, worst %.1fms)" % ("; ".join(parts), worst)
    if breaches:
        detail += " -- OVER on: %s" % ", ".join(breaches)
    if rc_failures:
        detail += " -- NONZERO RC: %s" % "; ".join(rc_failures)
    return ok, detail
