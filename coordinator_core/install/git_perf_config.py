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
warm. Do not add a key to `SETTINGS` without a measurement in that audit's shape.

WHY THIS IS PER-REPO AND NOT A GLOBAL STANZA. `core.untrackedCache` is not merely
configuration -- the cache it enables lives INSIDE `.git/index`. Setting the
config key alone does nothing until the index is extended, which is why
`apply()` runs `update-index --untracked-cache` and not just `config`. A global
`~/.gitconfig` line would set the key for every repo and populate none of them.

NEGATIVE SPEC -- this module does not:
  - clobber a value someone has deliberately set to something else; a differing
    existing value is REPORTED and left alone, never overwritten
  - enable anything on a filesystem that fails git's own mtime probe
  - start any daemon; `core.fsmonitor` is deliberately absent from `SETTINGS`
  - touch `~/.gitconfig`; a machine-global surface is shared across peers and a
    same-host write can strand one mid-sync
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional

#: Repo-local keys this module owns, and the value each must hold. Every entry
#: must cite a measurement -- see the module docstring.
SETTINGS: Dict[str, str] = {
    "core.untrackedCache": "true",
}


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
    """Apply every setting in `SETTINGS` to `repo`, idempotently.

    Returns a list of human-readable lines describing what happened -- one per
    setting, always, whether it changed, was already correct, was skipped, or was
    left alone because a peer had set it to something else. A caller that prints
    nothing on a no-op cannot tell "already correct" from "never ran".

    Idempotent by construction: a second call finds every value already correct
    and reports `ok` for each, changing nothing.
    """
    report: List[str] = []
    for key, wanted in sorted(SETTINGS.items()):
        current = _git(repo, "config", "--get", key).stdout.strip() or None

        if current == wanted:
            report.append("ok      %s = %s (already set)" % (key, wanted))
        elif current is not None:
            # NOT AN ERROR AND NOT OURS TO WIN. A peer machine may differ
            # deliberately; the negative spec forbids clobbering it.
            report.append(
                "left    %s = %s (differs from %s -- not overwritten)" % (key, current, wanted)
            )
            continue
        else:
            if key == "core.untrackedCache" and not filesystem_supports_untracked_cache(repo):
                report.append("skip    %s (filesystem failed git's mtime probe)" % key)
                continue
            if dry_run:
                report.append("would   %s = %s" % (key, wanted))
                continue
            proc = _git(repo, "config", key, wanted)
            if proc.returncode != 0:
                report.append("FAILED  %s: %s" % (key, proc.stderr.strip()))
                continue
            report.append("set     %s = %s" % (key, wanted))

        if key == "core.untrackedCache" and not dry_run:
            # THE CONFIG KEY ALONE IS INERT -- the cache lives in the index and
            # must be extended into it. Cheap and idempotent when already present.
            extend = _git(repo, "update-index", "--untracked-cache")
            if extend.returncode != 0:
                # Review: coordinator:code-reviewer -- this used to append a
                # SECOND report line for the same key, violating the
                # docstring's "one line per setting, always" contract that
                # test_every_setting_produces_a_report_line_always enforces.
                # Folded into the line already appended for this key instead.
                report[-1] += " (index not extended: %s)" % extend.stderr.strip()

    return report


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repo to configure")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    for line in apply(Path(args.repo).resolve(), dry_run=args.dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
