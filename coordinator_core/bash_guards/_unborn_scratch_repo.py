"""coordinator_core.bash_guards._unborn_scratch_repo -- the destructive-rm guard's one exemption
for removing a whole repo: a history-less repo under an enclosing repo's scratch/.

Spawn-free and stdlib-only: dispatch_checks imports it eagerly at negligible cost.
"""

from __future__ import annotations

import os


def is_unborn_scratch_repo(root: str) -> bool:
    """True when root is a non-bare repo inside a `scratch/` directory of an enclosing repo and
    its store holds no refs, packed refs, stash or reflog: deleting it loses only scratch files.

    TRAP: an empty refs/ tree is the only no-history proof; a repo with any ref, even unpushed,
    stays denied. Spawn-free.
    """
    gitdir = os.path.join(root, ".git")
    if not os.path.isdir(gitdir):
        return False
    parts = os.path.normpath(os.path.abspath(root)).split(os.sep)
    in_scratch = any(
        parts[i] == "scratch" and os.path.exists(os.sep.join(parts[:i] + [".git"]))
        for i in range(1, len(parts) - 1)
    )
    if not in_scratch:
        return False
    try:
        for _dirpath, _dirs, files in os.walk(os.path.join(gitdir, "refs")):
            if files:
                return False
        packed = os.path.join(gitdir, "packed-refs")
        if os.path.isfile(packed) and any(
            line.strip() and not line.startswith("#")
            for line in open(packed, encoding="utf-8", errors="replace")
        ):
            return False
    except OSError:
        return False
    return not os.path.exists(os.path.join(gitdir, "logs"))
