"""coordinator_core.git.ls_files_bytes -- the bytes-preserving sibling of
`ls_files.py`'s tracked-file enumerator, for callers doing a BYTE-EXACTNESS
probe over the tracked corpus (EOL declared-vs-actual drift detection being
the motivating one).

Why a sibling rather than a change to `ls_files.tracked_files`: that function
decodes with ``errors="replace"`` before splitting, which substitutes U+FFFD
on any non-UTF-8 path byte. For a probe whose whole predicate is "are these
bytes what the declaration says they are", a lossy decode is the bug it is
looking for. Five-plus call sites depend on `tracked_files`' ``Tuple[str, ...]``
return, so widening it in place is a separate and larger change; this module
adds a parallel path instead.

Mechanism is deliberately identical to `ls_files.py` -- same argv convention
(``[git, "-C", root, "ls-files", "-z", "--", pathspec]``), same
``stdin=DEVNULL``, ``timeout=10`` and ``no_console_creationflags()``, same
one-spawn-per-(repo_root, pathspec) ``lru_cache`` shape, same fold-to-empty
handling of a non-zero exit. It keeps its OWN cache rather than re-deriving
from `tracked_files`' cached value: by the time that tuple exists the bytes
are already gone.

Cache is a PROCESS-lifetime cache with no invalidation, matching the sibling
module; call ``_tracked_files_bytes_cached.cache_clear()`` for a fresh read
after a commit within one process.

COLD-PATH-ONLY (fix for a landed-review finding): that cache assumption is
correct for a one-shot CLI process, where "process lifetime" is one command
dispatch, but wrong for a caller registered as an op and therefore served
by a resident, multi-day warm server -- the first call against a given root
would otherwise pin that root's tracked-file list for the server's ENTIRE
life, with files added since invisible and files deleted since silently
dropped. `use_cache: bool = True` lets such a caller opt OUT per call
(``tracked_files_bytes(root, use_cache=False)``); the cache remains the
default because most callers ARE cold CLI processes. `eol.census` is the
first ``use_cache=False`` caller -- see that module's docstring.

NEGATIVE SPEC: this module does not modify, wrap, or deprecate
`ls_files.tracked_files`. Both are first-class; text callers keep the decoding
one.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md,
chunk C1 (AC2 -- "the census reads bytes end to end").
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Tuple, Union

from coordinator_core.win_portability import no_console_creationflags

__all__ = ["tracked_files_bytes"]


def tracked_files_bytes(
    repo_root: Union[str, Path], pathspec: str = ".", use_cache: bool = True
) -> Tuple[bytes, ...]:
    """Repo-relative paths tracked by git under ``repo_root`` matching
    ``pathspec``, as UNDECODED bytes -- forward-slash separated, exactly as
    git emitted them under ``-z``.

    Returns an empty tuple when git is absent, ``repo_root`` is not a
    repository, or the call times out; never raises for those cases.

    ``use_cache`` (default True) routes through the process-lifetime
    ``lru_cache``, correct for a one-shot CLI process. Pass False for a
    caller that is itself long-lived (a warm-served op) -- see the module
    docstring's "COLD-PATH-ONLY" note. A False call always re-spawns and is
    never memoized, independent of any prior or later cached call for the
    same (repo_root, pathspec).
    """
    resolved_root = str(Path(repo_root).resolve())
    if not use_cache:
        return _tracked_files_bytes_uncached(resolved_root, pathspec)
    return _tracked_files_bytes_cached(resolved_root, pathspec)


@lru_cache(maxsize=None)
def _tracked_files_bytes_cached(repo_root: str, pathspec: str) -> Tuple[bytes, ...]:
    return _tracked_files_bytes_uncached(repo_root, pathspec)


def _tracked_files_bytes_uncached(repo_root: str, pathspec: str) -> Tuple[bytes, ...]:
    git_bin = shutil.which("git")
    if git_bin is None:
        return ()
    try:
        proc = subprocess.run(
            [git_bin, "-C", repo_root, "ls-files", "-z", "--", pathspec],
            capture_output=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if proc.returncode != 0:
        stderr = proc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        print(
            f"git.ls_files_bytes: git -C {repo_root} ls-files -- {pathspec!r} exited "
            f"{proc.returncode} (treating as not-a-repo / no matches): "
            f"{(stderr or '').strip()}",
            file=sys.stderr,
        )
        return ()
    raw = proc.stdout
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    return tuple(entry for entry in raw_bytes.split(b"\x00") if entry)
