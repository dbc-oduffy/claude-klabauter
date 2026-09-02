"""coordinator_core.install.git_perf_config -- applies the git performance
settings a fresh clone would otherwise never get, so a repo is born with them
rather than acquiring them when someone notices.

WHAT IS ADOPTED, AND WHY ONLY ONE THING. Measured 2026-08-29 on a 35,454-file
worktree, child-process CPU, k=5 x n=11
(`state/audits/2026-08-29-git-config-warm-measurements.md`):

  core.untrackedCache   -50.0 ms p50 (-19%)   ADOPTED
  core.preloadIndex     -15.6 ms, inside noise -- `core.fscache` already covers it on Windows
  index.version=4       NO RELIABLE EFFECT at this sample size -- not adopted for want of
                        evidence, not because of it
  feature.manyFiles     UNMEASURED warm -- not rejected

RETRACTION (2026-08-29, `state/memo-outbox/sent/git-perf-index-version-claim-retracted.md`,
lesson `state/lessons/2026-08-29-a-sequential-a-b-benchmark-measures-position-not-treatment.yaml`).
This block previously read `index.version=4  +15.6 ms SLOWER` and rejected
`feature.manyFiles` by implication. That +15.6 ms was a measurement-order artifact:
a single sequential A->B with the treatment always second. Re-run A-B-A-B with the
order permuted, the sign flips (v4 43.7 ms slower measured second, 25.0 ms faster
measured first) and in both rounds the second-measured arm was slower whatever it
contained. Anyone deciding about v4 or `feature.manyFiles` starts from no evidence,
not from a rejection.

A settings list is not a performance strategy. Two of the four obvious knobs do
nothing here and two are unmeasured, and the only way to know either was to
measure warm, with the arm order permuted. Do not add another setting to this
module without a measurement in that audit's shape.

WHY THIS IS PER-REPO AND NOT A GLOBAL STANZA. `core.untrackedCache` is not merely
configuration -- the cache it enables lives INSIDE `.git/index`. Setting the
config key alone does nothing until the index is extended, which is why
`apply()` runs `update-index --untracked-cache` and not just `config`. A global
`~/.gitconfig` line would set the key for every repo and populate none of them.

FLEET SWEEP. `apply()` is per-repo. `apply_fleet()` joins it to the same
`repos.*` registry enumeration `ensure_hooks_fleet`
(`coordinator/bin/lib/git_hook_install.py`) uses for hooks, so every
registered worktree gets this config, not only whichever one repo an
installer happened to be invoked from -- see `apply_fleet`'s own docstring.
`iter_fleet_worktrees()` is that enumeration, factored out so
`workday-start-health-probes.py :: cmd_git_perf_currency` (a zero-spawn
health-probe caller with no reason to apply anything) can walk the same
fleet without re-deriving a second registry-enumeration scheme.

NEGATIVE SPEC -- this module does not:
  - clobber a value someone has deliberately set to something else; a differing
    existing value is REPORTED and left alone, never overwritten
  - enable anything on a filesystem that fails git's own mtime probe
  - start any daemon; `core.fsmonitor` is deliberately never applied here
  - touch `~/.gitconfig`; a machine-global surface is shared across peers and a
    same-host write can strand one mid-sync
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from coordinator_core.git.run import run_git
from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="git-perf-config",
    source_module="coordinator_core.install.git_perf_config",
    clauses=(
        # Clause 1 — `apply()`'s one adopted key, plus the index extension
        # the key alone is inert without (module docstring: "the cache
        # lives INSIDE `.git/index`"). Per-repo, per this module's own
        # negative spec: never `~/.gitconfig`, and a differing existing
        # value is reported and left alone, never overwritten.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="git-config-key",
                    key="core.untrackedCache",
                    reason="apply(): set true on a fs that passes git's own mtime probe, then extended into .git/index via `update-index --untracked-cache`",
                ),
            ),
        ),
        # Clause 2 — the three maintenance keys `_apply_maintenance_keys`
        # sets alongside clause 1, same idempotent/never-clobber contract.
        # Never unset on uninstall (module docstring: git's compiled
        # defaults resume harmlessly once the keys are gone).
        StaticClause(
            entries=(
                WriteSurfaceEntry(kind="git-config-key", key="maintenance.strategy"),
                WriteSurfaceEntry(kind="git-config-key", key="maintenance.auto"),
                WriteSurfaceEntry(kind="git-config-key", key="maintenance.prefetch.enabled"),
            ),
        ),
    ),
)


def filesystem_supports_untracked_cache(repo: Path) -> bool:
    """git's own mtime probe. Returns False rather than raising, so a filesystem
    that cannot carry the cache is a skip and never an install failure."""
    return run_git(["update-index", "--test-untracked-cache"], cwd=str(repo)).returncode == 0


def apply(repo: Path, *, dry_run: bool = False) -> List[str]:
    """Apply `core.untrackedCache` to `repo`, idempotently.

    Returns a single-element report line describing what happened -- whether it
    changed, was already correct, was skipped, or was left alone because a peer
    had set it to something else. A caller that prints nothing on a no-op cannot
    tell "already correct" from "never ran".

    Idempotent by construction: a second call finds the value already correct
    and reports `ok`, changing nothing.
    """
    # Review: coordinator:overengineering-reviewer -- this was a loop over a
    # one-entry SETTINGS dict, but the entry's real behaviour (the fs-probe gate
    # and the index-extension step below) was reached by two literal
    # key == "core.untrackedCache" checks inside the loop body, so the
    # abstraction never generalized. A second setting starts by re-reading the
    # module docstring's measurement bar, not by restoring the dict.
    key, wanted = "core.untrackedCache", "true"
    report: List[str] = []
    current_result = run_git(["config", "--get", key], cwd=str(repo))
    # Review: integrator sweep -- an unread current value (timeout/missing
    # git) must not be folded into "unset", which the branch below treats as
    # license to write. That would let a peer's deliberately differing value
    # (the negative spec's own "never overwritten" case) get clobbered simply
    # because this read never came back, not because it was confirmed absent.
    if current_result.timed_out or current_result.returncode == 127:
        report.append("skip    %s (could not read current value: git did not answer)" % key)
        return report
    current = current_result.stdout.strip() or None

    if current == wanted:
        report.append("ok      %s = %s (already set)" % (key, wanted))
    elif current is not None:
        # NOT AN ERROR AND NOT OURS TO WIN. A peer machine may differ
        # deliberately; the negative spec forbids clobbering it.
        report.append(
            "left    %s = %s (differs from %s -- not overwritten)" % (key, current, wanted)
        )
        return report
    else:
        if not filesystem_supports_untracked_cache(repo):
            report.append("skip    %s (filesystem failed git's mtime probe)" % key)
            return report
        if dry_run:
            report.append("would   %s = %s" % (key, wanted))
            return report
        proc = run_git(["config", key, wanted], cwd=str(repo))
        if proc.returncode != 0:
            report.append("FAILED  %s: %s" % (key, proc.stderr.strip()))
            return report
        report.append("set     %s = %s" % (key, wanted))

    if not dry_run:
        # THE CONFIG KEY ALONE IS INERT -- the cache lives in the index and
        # must be extended into it. Cheap and idempotent when already present.
        extend = run_git(["update-index", "--untracked-cache"], cwd=str(repo))
        if extend.returncode != 0:
            report[-1] += " (index not extended: %s)" % extend.stderr.strip()

    report.extend(_apply_maintenance_keys(repo, dry_run=dry_run))
    return report


# The three keys that hand maintenance to the ceremony leg. They live HERE and
# not in configure_git's _SETTINGS because they are actions taken against a
# repo at install time, not declarations -- the same reason
# `update-index --untracked-cache` lives here.
#
# maintenance.prefetch.enabled=false IS NOT IN THE ORIGINATING ASK. It is
# required by the spike: git's schedules CASCADE, so `prefetch` runs at the
# daily and weekly tiers as well as hourly, and it is the one task in this
# otherwise network-free design that goes to the network -- a `git fetch`
# against every remote plus two `gh auth git-credential` round-trips, 293.8 ms
# and 11.2 processes. With prefetch enabled the daily tier measures 575.0 ms
# and weekly 618.8 ms, both over the 500 ms brightline; with it disabled they
# are 190.6 ms and 178.1 ms.
#
# THE ALTERNATIVE NOT TAKEN: pinning daily and weekly to explicit `--task=`
# lists, the shape `git_maintenance` already gives hourly. The key wins because
# it is one key set once per repo, versus a task list that must be kept in sync
# with git's own strategy definition across git versions -- if a future git
# adds a task to `--schedule=daily`, the task-list shape silently drifts back
# open while the key shape does not.
#
# The key suppresses prefetch for ALL `git maintenance` invocations in this
# repo, including manual ones and future ones no coordinator code authors --
# not only the daily/weekly tiers the ceremony drives.
#
# THE TWO-WRITER ROLLOUT WINDOW, mirrored from configure_git._SETTINGS's own
# comment on `gc.auto`: `coordinator_core.ops.configure_git` writes `gc.auto=0`
# on a separate invocation path from this module's `apply()`/`apply_fleet()`.
# A repo can sit with `gc.auto=0` already written and these three maintenance
# keys still at git's defaults (`maintenance.auto` true, `prefetch.enabled`
# true) until this module's sweep reaches it -- an installer ordering where
# configure_git's Phase 1 runs before the fleet-sweep phase in
# `maximalist.py`, or a repo-setup-onboarded worktree awaiting its first
# fleet sweep. In that window `git maintenance run --auto`, including the
# network-touching `prefetch` task, keeps firing unconstrained. WHAT CLOSES
# IT: the daily workday-start ceremony's `git-perf-currency` health probe
# (`orient_assemble.readers_health_reaper :: _read_git_perf_currency`) --
# its `--fix` path calls `apply_fleet` in-process, which reaches these three
# keys via `apply()` on every registered worktree. The window is bounded to
# "until the next workday-start ceremony run," not indefinite; it is not
# transactional, and co-locating the two writers into one op is a design
# change beyond this module's scope.
#
# UNINSTALL DISPOSITION, stated rather than left silent: none of the three are
# unset on uninstall, and that is deliberate. git's compiled defaults
# (maintenance.strategy unset, maintenance.auto true, prefetch enabled) resume
# harmlessly the moment the keys are gone, and a coordinator-uninstalled
# worktree reverting to git's own defaults is what an uninstall is supposed to
# do -- not a residue needing its own removal step.
_MAINTENANCE_KEYS: tuple[tuple[str, str], ...] = (
    ("maintenance.strategy", "incremental"),
    ("maintenance.auto", "false"),
    ("maintenance.prefetch.enabled", "false"),
)


def _apply_maintenance_keys(repo: Path, *, dry_run: bool = False) -> List[str]:
    """Set the three maintenance keys in `repo`, idempotently.

    Honours this module's existing negative spec unchanged: a differing
    existing value is REPORTED and left alone, never overwritten. A peer
    machine may differ deliberately.

    NEVER `git maintenance register`. Setting the keys directly is strictly
    less: `register` additionally writes this repo's path into the
    multi-valued `maintenance.repo` key in the operator's GLOBAL config, which
    is read only by `git for-each-repo`, which only the scheduler runs, which
    this design never runs. It buys nothing and costs a machine-wide
    out-of-repo write surface.
    """
    report: List[str] = []
    for key, wanted in _MAINTENANCE_KEYS:
        current_result = run_git(["config", "--get", key], cwd=str(repo))
        # Review: integrator sweep -- same "unread is not unset" gap as
        # `apply()` above: a timeout must not license the write branch below.
        if current_result.timed_out or current_result.returncode == 127:
            report.append(
                "skip    %s (could not read current value: git did not answer)" % key
            )
            continue
        current = current_result.stdout.strip() or None
        if current == wanted:
            report.append("ok      %s = %s (already set)" % (key, wanted))
        elif current is not None:
            report.append(
                "left    %s = %s (differs from %s -- not overwritten)" % (key, current, wanted)
            )
        elif dry_run:
            report.append("would   %s = %s" % (key, wanted))
        else:
            proc = run_git(["config", key, wanted], cwd=str(repo))
            if proc.returncode != 0:
                report.append("FAILED  %s: %s" % (key, proc.stderr.strip()))
            else:
                report.append("set     %s = %s" % (key, wanted))
    return report


def _git_hook_install_registry_helpers():
    """Import `_registry_repo_roots`/`_classify_target` from
    `coordinator/bin/lib/git_hook_install.py`, which lives outside this
    package and therefore off `sys.path` by default.

    Mirrors `coordinator_core.ops.doctor._git_hook_install`'s own guarded
    import of the same module (same reason: that file is not a
    `coordinator_core` package member, so reaching it needs a `sys.path`
    push). Not imported directly from `doctor.py` -- that helper is
    module-private, and duplicating the ~10-line lookup here is cheaper than
    creating a coupling ACROSS PACKAGES on another module's leading-underscore
    name.

    THAT CLAUSE IS NARROWER THAN IT LOOKS, and the qualifier above is load-
    bearing: the lines below reach straight into `git_hook_install`'s OWN
    leading-underscore names. What is avoided is a `coordinator_core.install`
    -> `coordinator_core.ops` private coupling, not private names as such --
    `git_hook_install` has no public surface for this and is reached through a
    `sys.path` push either way. An earlier reading of this docstring took it
    for a blanket ban on importing a sibling's private helpers and read it as
    contradicting the same-package `_env_int`/`_mtime_epoch` imports in
    `coordinator_core.ops.git_maintenance`; it is not in tension with those,
    which are intra-package and carry no `sys.path` manipulation at all.

    Returns `None` on any failure (module not found, or found but
    missing an expected attribute), so a caller degrades to an advisory line
    rather than raising -- this runs at install time, on the machine whose
    layout may itself be incomplete.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "coordinator" / "bin" / "lib"
        if (cand / "git_hook_install.py").is_file():
            cand_str = str(cand)
            inserted = cand_str not in sys.path
            if inserted:
                sys.path.insert(0, cand_str)
            try:
                import git_hook_install  # noqa: PLC0415

                return git_hook_install._registry_repo_roots, git_hook_install._classify_target
            except Exception:
                return None
            finally:
                if inserted:
                    try:
                        sys.path.remove(cand_str)
                    except ValueError:
                        pass
    return None


class FleetWalkResult:
    """Zero-spawn result of `iter_fleet_worktrees` -- the enumerate/classify/
    skip-mirror/collect-missing half of a fleet walk, shared by `apply_fleet`
    and `workday-start-health-probes.py :: cmd_git_perf_currency` so that walk
    exists once rather than twice (see `iter_fleet_worktrees`'s docstring).

    `ok=False` means the walk itself could not run -- `reason` is one of
    `"helpers_unavailable"`, `"registry_error"` (with `detail` set to the
    exception text) or `"no_roots"`. `ok=True` means `items` is populated:
    each entry is `(kind, key, root)` for `kind in ("missing", "worktree")`,
    or `(kind, key, root, detail)` for `kind == "error"` (classify_target
    raised for that one root -- isolated per-item so one bad root cannot
    discard the walk).
    """

    __slots__ = ("ok", "reason", "detail", "roots_count", "items")

    def __init__(self, *, ok, reason=None, detail=None, roots_count=0, items=None):
        self.ok = ok
        self.reason = reason
        self.detail = detail
        self.roots_count = roots_count
        self.items = items if items is not None else []


def iter_fleet_worktrees(bin_dir: Path) -> "FleetWalkResult":
    """Zero-spawn enumeration of every registered `worktree` repo, shared by
    `apply_fleet` (which applies to each) and `cmd_git_perf_currency` (which
    reads each `.git/config`) -- the only difference that was ever real
    between the two callers. Neither `_registry_repo_roots` nor
    `_classify_target` spawns a process; this function stays zero-spawn end
    to end so the health-probe caller can run it inside the 500ms
    `/workday-start` brightline.

    `mirror` targets are silently, permanently skipped -- see
    `_classify_target`'s own docstring. `missing` targets (a registry entry
    whose path is gone or was never a git repo) are surfaced as `"missing"`
    items, because that is a broken registry entry, not a healthy no-op.
    `classify_target` raising for one root is isolated into an `"error"`
    item rather than aborting the walk, so one bad registry entry cannot
    discard the results already collected for the roots before it.

    Never raises: an unresolvable helper import or an unreadable registry
    both degrade to `ok=False` rather than propagating.
    """
    helpers = _git_hook_install_registry_helpers()
    if helpers is None:
        return FleetWalkResult(ok=False, reason="helpers_unavailable")

    registry_repo_roots, classify_target = helpers

    try:
        roots = registry_repo_roots(str(bin_dir))
    except Exception as exc:  # defensive: registry I/O must never abort install
        return FleetWalkResult(ok=False, reason="registry_error", detail=str(exc))

    if not roots:
        return FleetWalkResult(ok=False, reason="no_roots")

    items: List[tuple] = []
    for key, root in sorted(roots):
        try:
            kind = classify_target(root)
        except Exception as exc:
            items.append(("error", key, root, str(exc)))
            continue
        if kind == "mirror":
            continue
        if kind == "missing":
            items.append(("missing", key, root))
            continue
        items.append(("worktree", key, root))

    return FleetWalkResult(ok=True, roots_count=len(roots), items=items)


def apply_fleet(bin_dir: Path, *, dry_run: bool = False) -> List[str]:
    """Apply `core.untrackedCache` to every registered `worktree` repo on this machine.

    WHY THIS EXISTS. `apply()` above is per-repo, and until now was called on
    exactly one repo (the claude-klabauter root, from `scripts/setup.py`) -- every
    other registered repo on the box never got `core.untrackedCache` at all.
    That gap is permanent, not one-time: the config key lives INSIDE
    `.git/index` (see module docstring), and since the session-init hook was
    removed 2026-07-15 nothing re-applies git config to an already-registered
    repo either. `ensure_hooks_fleet` (`coordinator/bin/lib/git_hook_install.py`)
    already solved exactly this drift class for hooks by sweeping every
    `repos.*` registry entry instead of the one repo a caller happened to be
    standing in; this function joins that sweep to `apply()` instead of
    re-deriving a second registry-enumeration scheme.

    REUSES `_registry_repo_roots`/`_classify_target` from `git_hook_install`
    (via `_git_hook_install_registry_helpers`) rather than
    `~/.claude/working-repos.yaml` -- that YAML is a competing, hand-maintained
    source consumed by `/repo-setup --batch` and is explicitly not this
    module's source of truth.

    Applies to `worktree` targets only. `mirror` targets (e.g. an outward
    publish mirror like claude-klabauter) are silently, permanently skipped --
    `_classify_target`'s own docstring explains why reporting a permanent,
    correct exclusion on every run is how an operator learns to ignore the
    output. `missing` targets (registry entry whose path is gone or was never
    a git repo) ARE reported, because that is a broken registry entry, not a
    healthy no-op.

    Returns one report line per repo (each itself carrying the one line
    `apply()` returns), plus a summary line. Never raises: an
    unresolvable helper import, an unreadable registry, or a single repo's
    `classify_target`/`apply()` raising unexpectedly (e.g. `git` absent from
    PATH) all degrade to a report line -- the per-repo loop body is wrapped so
    one bad repo cannot discard the report already accumulated for the repos
    before it. This is an install-time sweep, never a gate.

    REUSES `iter_fleet_worktrees` for the enumerate/classify/skip-mirror/
    collect-missing half; only the per-worktree action (`apply()`) and this
    function's own report wording are its own.
    """
    report: List[str] = []

    walk = iter_fleet_worktrees(bin_dir)
    if not walk.ok:
        if walk.reason == "helpers_unavailable":
            report.append(
                "advisory: git_hook_install registry helpers unavailable -- "
                "configured nothing fleet-wide (per-repo apply() still ran wherever "
                "its own caller invoked it directly)."
            )
        elif walk.reason == "registry_error":
            report.append(
                f"advisory: could not read repo registry ({walk.detail}) -- configured nothing fleet-wide."
            )
        else:  # no_roots
            report.append(
                "found no registered repos -- configured nothing; this is not the "
                "same fact as 'every repo is current'."
            )
        return report

    applied_repos = 0
    for item in walk.items:
        kind, key, root = item[0], item[1], item[2]
        if kind == "missing":
            report.append(f"missing  {key} -> {root} (registry entry unreachable, not a git repo)")
            continue
        if kind == "error":
            # Review: coordinator:code-reviewer -- classify_target() is not
            # wrapped by apply()'s own returncode handling for a raise from
            # subprocess.run itself (e.g. FileNotFoundError if git is absent
            # from PATH). Isolated per-repo (inside iter_fleet_worktrees) so
            # one bad repo degrades to a FAILED line instead of losing the
            # whole report.
            report.append(f"FAILED  {key}: {item[3]}")
            continue
        try:
            applied_repos += 1
            for line in apply(Path(root), dry_run=dry_run):
                report.append(f"{key}: {line}")
        except Exception as exc:
            # apply() itself is not expected to raise (it wraps its own git
            # calls via CompletedProcess/returncode), but isolated the same
            # way as classify_target above so one bad repo cannot discard the
            # report already accumulated for the repos before it.
            report.append(f"FAILED  {key}: {exc}")
            continue

    report.append(
        f"fleet summary: swept {walk.roots_count} registered repo(s), applied to {applied_repos} worktree(s)."
    )
    return report


# Review: coordinator:overengineering-reviewer -- dropped the `main()` /
# `__main__` CLI entrypoint. Its two real callers (scripts/setup.py::
# apply_git_perf_config and maximalist.py Step 3.5a.1c) both import and call
# apply()/apply_fleet() in-process; nothing names an operator invoking
# `python -m coordinator_core.install.git_perf_config`, and the CLI only ever
# reached apply(), never the apply_fleet() sweep that is the actual
# deliverable. Dropping is less code than adding a --fleet flag nobody asked
# for.
