"""coordinator_core.install.git_perf_config -- applies the git performance
settings a fresh clone would otherwise never get, so a repo is born with them
rather than acquiring them when someone notices.

WHAT IS ADOPTED, AND WHY ONLY ONE THING. Measured 2026-08-29 on a 35,454-file
worktree, child-process CPU, k=5 x n=11
(`state/audits/2026-08-29-git-config-warm-measurements.md`):

  core.untrackedCache   -50.0 ms p50 (-19%)   ADOPTED
  core.preloadIndex     -15.6 ms, inside noise -- `core.fscache` already covers it on Windows
  index.version=4       +15.6 ms SLOWER -- shrinks the index 35%, pays for it in CPU on every read
  feature.manyFiles     rejected by implication: it turns on index.version=4

A settings list is not a performance strategy. Three of the four obvious knobs
do nothing or harm here, and the only way to know that was to measure each one
warm. Do not add another setting to this module without a measurement in that
audit's shape.

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

NEGATIVE SPEC -- this module does not:
  - clobber a value someone has deliberately set to something else; a differing
    existing value is REPORTED and left alone, never overwritten
  - enable anything on a filesystem that fails git's own mtime probe
  - start any daemon; `core.fsmonitor` is deliberately never applied here
  - touch `~/.gitconfig`; a machine-global surface is shared across peers and a
    same-host write can strand one mid-sync
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List


def _no_window_flags() -> int:
    """Windows: keep a console from flashing for each probe under a headless host."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", *args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=_no_window_flags(),
    )


def filesystem_supports_untracked_cache(repo: Path) -> bool:
    """git's own mtime probe. Returns False rather than raising, so a filesystem
    that cannot carry the cache is a skip and never an install failure."""
    return _git(repo, "update-index", "--test-untracked-cache").returncode == 0


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
    current = _git(repo, "config", "--get", key).stdout.strip() or None

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
        proc = _git(repo, "config", key, wanted)
        if proc.returncode != 0:
            report.append("FAILED  %s: %s" % (key, proc.stderr.strip()))
            return report
        report.append("set     %s = %s" % (key, wanted))

    if not dry_run:
        # THE CONFIG KEY ALONE IS INERT -- the cache lives in the index and
        # must be extended into it. Cheap and idempotent when already present.
        extend = _git(repo, "update-index", "--untracked-cache")
        if extend.returncode != 0:
            report[-1] += " (index not extended: %s)" % extend.stderr.strip()

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
    creating a cross-module coupling on another module's leading-underscore
    name. Returns `None` on any failure (module not found, or found but
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
    """
    report: List[str] = []

    helpers = _git_hook_install_registry_helpers()
    if helpers is None:
        report.append(
            "advisory: git_hook_install registry helpers unavailable -- "
            "configured nothing fleet-wide (per-repo apply() still ran wherever "
            "its own caller invoked it directly)."
        )
        return report

    registry_repo_roots, classify_target = helpers

    try:
        roots = registry_repo_roots(str(bin_dir))
    except Exception as exc:  # defensive: registry I/O must never abort install
        report.append(f"advisory: could not read repo registry ({exc}) -- configured nothing fleet-wide.")
        return report

    if not roots:
        report.append(
            "found no registered repos -- configured nothing; this is not the "
            "same fact as 'every repo is current'."
        )
        return report

    applied_repos = 0
    for key, root in sorted(roots):
        try:
            kind = classify_target(root)
            if kind == "mirror":
                continue
            if kind == "missing":
                report.append(f"missing  {key} -> {root} (registry entry unreachable, not a git repo)")
                continue
            applied_repos += 1
            for line in apply(Path(root), dry_run=dry_run):
                report.append(f"{key}: {line}")
        except Exception as exc:
            # Review: coordinator:code-reviewer -- classify_target()/apply()
            # are not wrapped by apply()'s own returncode handling for a raise
            # from subprocess.run itself (e.g. FileNotFoundError if git is
            # absent from PATH). Left unguarded, that raise would propagate
            # out of apply_fleet and discard every report line accumulated
            # for repos before this one. Isolated per-repo so one bad repo
            # degrades to a FAILED line instead of losing the whole report.
            report.append(f"FAILED  {key}: {exc}")
            continue

    report.append(
        f"fleet summary: swept {len(roots)} registered repo(s), applied to {applied_repos} worktree(s)."
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
