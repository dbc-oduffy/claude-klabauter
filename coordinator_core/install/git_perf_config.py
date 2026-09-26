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
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="git-config-key",
                    key="core.untrackedCache",
                    reason="apply(): set true on a fs that passes git's own mtime probe, then extended into .git/index via `update-index --untracked-cache`",
                ),
            ),
        ),
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
    return run_git(["update-index", "--test-untracked-cache"], cwd=str(repo)).returncode == 0


def apply(repo: Path, *, dry_run: bool = False) -> List[str]:
    # one-entry SETTINGS dict, but the entry's real behaviour (the fs-probe gate
    key, wanted = "core.untrackedCache", "true"
    report: List[str] = []
    current_result = run_git(["config", "--get", key], cwd=str(repo))
    # license to write. That would let a peer's deliberately differing value
    if current_result.timed_out or current_result.returncode == 127:
        report.append("skip    %s (could not read current value: git did not answer)" % key)
        return report
    current = current_result.stdout.strip() or None

    if current == wanted:
        report.append("ok      %s = %s (already set)" % (key, wanted))
    elif current is not None:
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
        extend = run_git(["update-index", "--untracked-cache"], cwd=str(repo))
        if extend.returncode != 0:
            report[-1] += " (index not extended: %s)" % extend.stderr.strip()

    report.extend(_apply_maintenance_keys(repo, dry_run=dry_run))
    return report


# not in configure_git's _SETTINGS because they are actions taken against a
# maintenance.prefetch.enabled=false IS NOT IN THE ORIGINATING ASK. It is
# THE ALTERNATIVE NOT TAKEN: pinning daily and weekly to explicit `--task=`
# THE TWO-WRITER ROLLOUT WINDOW, mirrored from configure_git._SETTINGS's own
# UNINSTALL DISPOSITION, stated rather than left silent: none of the three are
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

    __slots__ = ("ok", "reason", "detail", "roots_count", "items")

    def __init__(self, *, ok, reason=None, detail=None, roots_count=0, items=None):
        self.ok = ok
        self.reason = reason
        self.detail = detail
        self.roots_count = roots_count
        self.items = items if items is not None else []


def iter_fleet_worktrees(bin_dir: Path) -> "FleetWalkResult":
    helpers = _git_hook_install_registry_helpers()
    if helpers is None:
        return FleetWalkResult(ok=False, reason="helpers_unavailable")

    registry_repo_roots, classify_target = helpers

    try:
        roots = registry_repo_roots(str(bin_dir))
    except Exception as exc:
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
        else:
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
            report.append(f"FAILED  {key}: {item[3]}")
            continue
        try:
            applied_repos += 1
            for line in apply(Path(root), dry_run=dry_run):
                report.append(f"{key}: {line}")
        except Exception as exc:
            report.append(f"FAILED  {key}: {exc}")
            continue

    report.append(
        f"fleet summary: swept {walk.roots_count} registered repo(s), applied to {applied_repos} worktree(s)."
    )
    return report

