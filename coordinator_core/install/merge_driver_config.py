"""coordinator_core.install.merge_driver_config -- sets the DoE rule-class-register
merge driver in a DoE clone's local git config.

git runs a custom merge driver only when the clone's local config names it;
without the key every DoE clone falls back to a plain line merge on that file.

NEGATIVE SPEC -- this module does not:
  - clobber a differing existing value; it is REPORTED and left alone
  - touch a repo that is not a DoE clone (no coordinator/bin/merge-rule-class-register.py)
  - author the driver script or `.gitattributes`; coordinator-content-repo owns both
  - touch `~/.gitconfig`; only `--local` is read or written
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from coordinator_core.git.run import run_git
from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

KEY = "merge.rule-class-register.driver"
WANTED = "python3 coordinator/bin/merge-rule-class-register.py %O %A %B"
DRIVER_RELPATH = ("coordinator", "bin", "merge-rule-class-register.py")

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="merge-driver-config",
    source_module="coordinator_core.install.merge_driver_config",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="git-config-key",
                    key=KEY,
                    reason="apply(): set in a DoE clone's local config when absent; a differing value is left alone",
                ),
            ),
        ),
    ),
)


def is_doe_clone(repo: Path) -> bool:
    """True when `repo` carries the DoE merge-driver script."""
    return repo.joinpath(*DRIVER_RELPATH).is_file()


def apply(repo: Path, *, dry_run: bool = False) -> List[str]:
    """Set KEY in `repo`'s local config; return report lines.

    Idempotent. Non-DoE repos get a `skip` line and no write. An unread current
    value (git timeout/missing) is never treated as unset.
    """
    if not is_doe_clone(repo):
        return ["skip    %s (not a DoE clone)" % KEY]

    cur = run_git(["config", "--local", "--get", KEY], cwd=str(repo))
    if cur.timed_out or cur.returncode not in (0, 1):
        return ["skip    %s (could not read current value: git did not answer)" % KEY]
    current = cur.stdout.strip() or None

    if current == WANTED:
        return ["ok      %s = %s (already set)" % (KEY, WANTED)]
    if current is not None:
        return ["left    %s = %s (differs from %s -- not overwritten)" % (KEY, current, WANTED)]
    if dry_run:
        return ["would   %s = %s" % (KEY, WANTED)]
    proc = run_git(["config", "--local", KEY, WANTED], cwd=str(repo))
    if proc.returncode != 0:
        return ["FAILED  %s: %s" % (KEY, proc.stderr.strip())]
    return ["set     %s = %s" % (KEY, WANTED)]
