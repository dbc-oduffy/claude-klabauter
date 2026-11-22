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


_READONLY = 0x1  # FILE_ATTRIBUTE_READONLY
_REPARSE_POINT = 0x400  # symlink or junction: removed as a leaf, never descended into.


def _remove(func: Callable[[str], Any], path: str) -> bool:
    """`func(path)`, retried once after `make_removable` on `path` and (POSIX) its parent.

    True when `path` is gone afterwards (a missing path counts), False when the retry also failed.
    """
    try:
        func(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass
    for target in (path, None if _IS_WINDOWS else os.path.dirname(path)):
        if target:
            try:
                make_removable(target)
            except OSError:
                pass
    try:
        func(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _remove_leaf(path: str, is_dir: bool) -> None:
    """Remove a non-descended entry: a file or symlink by unlink; a Windows directory link by unlink, else rmdir."""
    if not _remove(os.unlink, path) and is_dir:
        _remove(os.rmdir, path)


def fast_rmtree(path: "str | os.PathLike[str]") -> bool:
    """Delete the tree at `path` (a file or link is removed as itself); never raises.

    One scandir walk. On Windows the read-only attribute comes free with the scandir entry, so a
    read-only file is made writable before its single unlink instead of failing the unlink, chmod'ing
    and retrying (three syscalls per file). Returns True when `path` no longer exists, False when a
    held handle or a permission left something behind.
    """
    root = os.fspath(path)
    try:
        top = os.lstat(root)
    except FileNotFoundError:
        return True
    except OSError:
        return not os.path.lexists(root)
    top_is_dir = stat.S_ISDIR(top.st_mode)
    if not top_is_dir or getattr(top, "st_file_attributes", 0) & _REPARSE_POINT:
        _remove_leaf(root, top_is_dir)
        return not os.path.lexists(root)
    dirs: list[str] = []
    pending = [root]
    while pending:
        current = pending.pop()
        dirs.append(current)
        try:
            scan = os.scandir(current)
        except OSError:
            try:
                make_removable(current)
                scan = os.scandir(current)
            except OSError:
                continue
        try:
            with scan:
                for entry in scan:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        _remove(os.unlink, entry.path)
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    is_dir = stat.S_ISDIR(st.st_mode)
                    if is_dir and not attrs & _REPARSE_POINT:
                        pending.append(entry.path)
                        continue
                    if attrs & _READONLY:
                        try:
                            os.chmod(entry.path, stat.S_IWRITE)
                        except OSError:
                            pass
                    _remove_leaf(entry.path, is_dir)
        except OSError:
            pass  # an iteration error mid-directory leaves the rest; the rmdir pass reports False
    for directory in reversed(dirs):
        _remove(os.rmdir, directory)
    return not os.path.lexists(root)


def rmtree_kwargs() -> dict:
    """Keyword args wiring `rmtree_retry` into `shutil.rmtree` (`onexc` on 3.12+, else `onerror`)."""
    key = "onexc" if sys.version_info >= (3, 12) else "onerror"
    return {key: rmtree_retry}
