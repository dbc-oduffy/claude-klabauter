"""Owner-safe rmtree retry helpers.

`stat.S_IWRITE` is 0o200: chmod'ing a directory to exactly that on POSIX leaves it write-only
and untraversable. These helpers add owner bits instead of replacing the mode.
"""
from __future__ import annotations

import os
import stat
import sys
from typing import Any, Callable

_IS_WINDOWS = os.name == "nt"


def make_removable(path: "str | os.PathLike[str]") -> None:
    """Make `path` deletable without ever dropping an owner bit.

    Windows: clear read-only (S_IWRITE). POSIX: directories gain u+rwx, files gain u+w; existing
    bits are kept. Raises OSError if the path cannot be chmod'ed (including a missing path).
    """
    if _IS_WINDOWS:
        os.chmod(path, stat.S_IWRITE)
        return
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return  # chmod follows links: it would rewrite the target's mode from the link's.
    add = 0o700 if stat.S_ISDIR(mode) else 0o200
    os.chmod(path, stat.S_IMODE(mode) | add)


def rmtree_retry(func: Callable[[str], Any], path: str, _exc: Any) -> None:
    """`shutil.rmtree` onerror/onexc handler: make `path` (and on POSIX its parent, whose write bit
    gates unlinking `path`) removable, then retry `func(path)`."""
    make_removable(path)
    if not _IS_WINDOWS:
        parent = os.path.dirname(path)
        if parent:
            try:
                make_removable(parent)
            except OSError:
                pass
    func(path)


def rmtree_kwargs() -> dict:
    """Keyword args wiring `rmtree_retry` into `shutil.rmtree` (`onexc` on 3.12+, else `onerror`)."""
    key = "onexc" if sys.version_info >= (3, 12) else "onerror"
    return {key: rmtree_retry}
