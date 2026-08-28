"""Filesystem-truth identity for directory paths, for dedup keys and
"is this the same tree?" comparisons.

WHY THIS EXISTS. Three call sites in the discovery -> registration ->
singularity chain established directory identity with STRING equality over a
normalized path. That is correct on case-sensitive POSIX and wrong everywhere
else: macOS ships case-INSENSITIVE APFS by default and Windows is
case-insensitive by default, so `~/code/repo` and `~/Code/repo` are one
directory with two spellings. `os.path.realpath` / `Path.resolve()` do NOT
normalize case on macOS, so canonicalization does not rescue a string
compare. Reported externally against the publish mirror as
dbc-oduffy/claude-klabauter#2, on a first-time macOS install.

WHY NOT `casefold()`. Lowercasing the path is wrong in the other direction:
on case-sensitive POSIX `~/code/repo` and `~/Code/repo` are two genuinely
different directories, and folding them collapses a real distinction. Any
fix shaped as "fold case when the platform is case-insensitive" also has to
decide what the platform is -- and that is a per-FILESYSTEM property, not a
per-platform one (a case-sensitive volume mounted on macOS, a case-sensitive
directory on Windows via `fsutil`). `os.name` cannot answer it.

THE SHAPE, AND WHY THIS ONE: ask the filesystem. `st_dev`/`st_ino` is the
identity the filesystem itself maintains, so it is right on all three
platforms without probing any of them, without a temp file, and without a
per-platform branch. Two spellings of one directory share an inode; two
directories that merely look alike do not.

Negative spec: this module never folds case, never consults `os.name`, and
never spawns. One `os.stat` per call is the entire cost -- see `CLAUDE.md`
§ The brightline (process time and spawn count are the measured axes; a
stat is neither).
"""

from __future__ import annotations

import os
from typing import Tuple, Union

#: A directory's identity: the filesystem's own `(st_dev, st_ino)` when it
#: could be stat'd, else the caller's normalized-string fallback. Both are
#: hashable and never compare equal to one another, so a single `set` or
#: `dict` can hold a mix of the two without a stat'd path ever colliding
#: with an un-stat'd one.
DirIdentity = Union[Tuple[int, int], str]


def dir_identity(path: str, *, fallback: str) -> DirIdentity:
    """The filesystem's identity for `path`, or `fallback` if it cannot be
    established.

    `fallback` is returned -- rather than raising or inventing a key -- for
    every path that cannot be stat'd: one that does not exist, one on a
    filesystem that reports no usable inode, or one that vanished between
    enumeration and this call (a real race on a shared tree). Callers pass
    their existing normalized string key, so a failure to stat degrades to
    exactly the pre-existing string-comparison behavior for that one path
    instead of dropping it or merging it with an unrelated one.

    An `st_ino` of 0 is treated as absent rather than as an identity: it is
    what some filesystems report when they do not track inodes, and every
    such path would otherwise collapse onto a single shared key -- merging
    unrelated directories, which is worse than the bug this replaces.
    """
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return fallback
    if not st.st_ino:
        return fallback
    return (st.st_dev, st.st_ino)


def same_dir(a: str, b: str) -> bool:
    """Whether `a` and `b` name the same directory on disk.

    Filesystem identity first (`os.path.samefile`), falling back to a
    `normcase`/`normpath` string comparison only when the stat cannot be
    taken -- which is the pre-existing behavior at every call site this
    replaces, preserved so an unreadable path degrades rather than
    reporting a confident False.
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
            os.path.normpath(b)
        )
