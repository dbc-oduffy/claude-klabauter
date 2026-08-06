"""coordinator_core.git.ls_files -- the one cached, direct-argv ``git
ls-files`` enumerator, for callers that need the TRACKED-FILE LIST under a
pathspec (as opposed to `git_dir.py`'s no-subprocess gitdir resolution or
`divergence.py`'s index/worktree diff predicate).

Why this exists: CLAUDE.md's Runtime conventions treats a cold `git` spawn on
the commit/session hot path as break-class, because Windows process creation
is expensive -- and several repo-wide static guards/tests enumerate the
tracked corpus via `git ls-files` but RE-SPAWN per call site instead of
resolving it once (`coordinator_core/tests/test_baton_class_is_the_only_
membership_set.py::_iter_py_files`, `coordinator_core/test_bin_launcher_
parity.py::_tracked_files`, called once per SCAN_ROOTS root/parametrized
test). `install/substrate.py::_resolve_directory_tracked_set` and
`install/uninstall_legs.py` already state and follow the correct norm ("one
`git ls-files` spawn total, never per file") for their own single-directory
probe; this module generalizes the same norm into a REUSABLE, cross-call-site
primitive keyed on (repo_root, pathspec) so unrelated callers querying the
same tree do not each pay their own spawn.

Mechanism: `functools.lru_cache` over a module-level function taking only
hashable (str, str) arguments, direct argv exec (no shell, no bash wrapper --
never routes through `bash.exe`, which on MinGit/git-bash is a cold shell per
call and the exact Windows-hostile shape this module eliminates), `-z`/NUL-
parsed so newline-splitting cannot break on an exotic (embedded-newline)
path. A non-zero exit (no such pathspec match failure aside -- ``git
ls-files`` itself does not fail on an empty match set, only when ``repo_root``
is not a git repository at all, or a malformed pathspec) is handled
explicitly, mirroring `substrate.py`'s "not a repo" precedent: reported to
stderr and folded to an empty tuple, never raised -- a guard/test consuming
this must not crash the whole run because one probed directory happens not to
be a git repository.

Cache is a PROCESS-lifetime cache with no invalidation: correct for
guards/tests that enumerate a tree once during a single process run (a
committed file mid-run would not be picked up) -- if a future caller needs a
fresh read after a commit within the same process, call
`tracked_files.cache_clear()`.

Spec backlink: cross-repo dispatch brief "cache the repeated `git ls-files`
enumerator spawn" (2026-08-05), propagating the norm already stated at
`install/substrate.py` ~line 2221 and `install/uninstall_legs.py` ~line 454.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Tuple, Union

__all__ = ["tracked_files"]

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tracked_files(repo_root: Union[str, Path], pathspec: str = ".") -> Tuple[str, ...]:
    """Repo-relative, forward-slash paths tracked by git under ``repo_root``
    matching ``pathspec`` (e.g. ``"*.py"``, a directory like
    ``"coordinator/bin"``, or the default ``"."`` for the whole tree).

    One `git ls-files -z` spawn per distinct (repo_root, pathspec) pair for
    the lifetime of the process -- see module docstring. Returns an empty
    tuple (never raises) when git is not on PATH, the spawn itself fails or
    times out, or ``repo_root`` is not a git repository at all -- every one
    of those is a degrade case a caller must treat as "nothing enumerable",
    not a crash.
    """
    resolved_root = str(Path(repo_root).resolve())
    return _tracked_files_cached(resolved_root, pathspec)


@lru_cache(maxsize=None)
def _tracked_files_cached(repo_root: str, pathspec: str) -> Tuple[str, ...]:
    git_bin = shutil.which("git")
    if git_bin is None:
        return ()
    try:
        proc = subprocess.run(
            [git_bin, "-C", repo_root, "ls-files", "-z", "--", pathspec],
            capture_output=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if proc.returncode != 0:
        stderr = proc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        print(
            f"git.ls_files: git -C {repo_root} ls-files -- {pathspec!r} exited "
            f"{proc.returncode} (treating as not-a-repo / no matches): "
            f"{(stderr or '').strip()}",
            file=sys.stderr,
        )
        return ()
    raw = proc.stdout
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return tuple(entry for entry in text.split("\x00") if entry)
