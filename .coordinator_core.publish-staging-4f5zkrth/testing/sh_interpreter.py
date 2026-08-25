"""Shared POSIX `sh` interpreter resolution for the pre-commit-hook test
family.

The hook-installer test suites (`coordinator_core/ops/test_install_*.py`,
`coordinator_core/install/test_dep_check.py`) behaviorally execute a
generated hook body via `sh` against stub gate scripts — the property under
test (exit-code clamping, `sh -n` syntax validity) can only be demonstrated
by real subprocess execution, not a textual/mocked check. Every one of those
spawn sites had `/bin/sh` hard-coded as the interpreter path, which does not
exist on a Windows host — the generated hook's shebang is correctly
POSIX (`#!/bin/sh`, since git-for-windows execs it via its own bundled
`sh`), but the *test's own spawn* naming that literal path is a
platform-portability bug, not a product requirement. Windows hosts with
git-for-windows installed carry a real `sh.exe`, so the fix is
capability-resolution, not a skip-on-Windows blanket.

`shutil.which("sh")` alone is not enough: git-for-windows does not put
`sh.exe` on `PATH` by default (only `Git/cmd` and `Git/mingw64/bin` are —
`Git/bin`, where `sh.exe` lives, deliberately is not, to avoid shadowing
POSIX tools like `find`/`sort` ahead of native Windows ones). The fallback
derives git's own install root from `shutil.which("git")` and checks the
two well-known git-for-windows locations underneath it
(`bin/sh.exe`, `usr/bin/sh.exe`) directly.

``SH_INTERPRETER`` is resolved once at import time — mirrors
``coordinator_core.testing.symlink_capability``'s single-probe-per-process
shape for the same reason: many call sites share the fast tier.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import pytest


def _resolve_sh_interpreter() -> Optional[str]:
    found = shutil.which("sh")
    if found:
        return found
    if Path("/bin/sh").exists():
        return "/bin/sh"
    git_path = shutil.which("git")
    if git_path:
        # git.exe normally resolves to <root>/cmd/git.exe or <root>/bin/git.exe;
        # `sh.exe` lives at <root>/bin/sh.exe and <root>/usr/bin/sh.exe, neither
        # of which git-for-windows adds to PATH.
        git_root = Path(git_path).resolve().parent.parent
        for candidate in (git_root / "bin" / "sh.exe", git_root / "usr" / "bin" / "sh.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


SH_INTERPRETER: Optional[str] = _resolve_sh_interpreter()
"""Absolute path to a POSIX `sh` interpreter, or ``None`` if this host has
none reachable via `PATH` or `/bin/sh`. Resolved once at import time."""


def require_sh_interpreter() -> str:
    """Return :data:`SH_INTERPRETER`, or skip the calling test if this host
    has no `sh` interpreter reachable at all.

    A skip states honestly that the check did not run; it must never read as
    a mysterious `FileNotFoundError`/`WinError 2`, and must never silently
    pass in its place.
    """
    if SH_INTERPRETER is None:
        pytest.skip(
            "no POSIX sh interpreter found (shutil.which('sh') and /bin/sh both absent)"
        )
    return SH_INTERPRETER
