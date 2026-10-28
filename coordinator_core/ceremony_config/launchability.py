"""Launchability check for an argv-only post-command: can this OS exec argv[0] directly.

Stdlib only. Resolution never runs the command. On Windows the test is CreateProcess
launchability, not PATH resolution: only a resolved `.exe`/`.com` launches, so a `.cmd`/`.bat`/
extensionless shim that `shutil.which` finds is still not launchable.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import List, Optional

__all__ = ["check", "resolve", "is_unlaunchable_windows_suffix"]

_WIN_LAUNCHABLE_SUFFIXES = (".exe", ".com")


def resolve(program: str, *, platform: str = sys.platform) -> Optional[str]:
    """Resolve `program` to a path: as given when it has a separator, else via PATH. None if unresolved.

    On win32 a suffixless separator-bearing path resolves to `<path>.exe` when that exists, as CreateProcess appends it.
    """
    if os.sep in program or (os.altsep and os.altsep in program):
        if platform == "win32" and not os.path.splitext(program)[1] and os.path.isfile(program + ".exe"):
            return program + ".exe"
        return program if os.path.isfile(program) else None
    return shutil.which(program)


def is_unlaunchable_windows_suffix(path: str) -> bool:
    """True when `path`'s suffix is one CreateProcess cannot launch."""
    return os.path.splitext(path)[1].casefold() not in _WIN_LAUNCHABLE_SUFFIXES


def check(argv: List[str], *, platform: str = sys.platform) -> Optional[str]:
    """Return None when `argv[0]` is launchable on `platform`, else a reason string. Runs nothing."""
    if not argv:
        return "empty argv"
    resolved = resolve(argv[0], platform=platform)
    if resolved is None:
        return "not found on PATH"
    if platform == "win32":
        if is_unlaunchable_windows_suffix(resolved):
            suffix = os.path.splitext(resolved)[1] or "(none)"
            return f"not launchable: '{resolved}' has suffix {suffix}, and only .exe/.com launch directly"
        return None
    # which() on a path applies the same X_OK test the platform launcher does.
    if os.path.isfile(resolved) and shutil.which(resolved) is not None:
        return None
    return f"not launchable: '{resolved}' is not an executable file"
