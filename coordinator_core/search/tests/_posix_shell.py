
from __future__ import annotations

import os
import shutil

import pytest
from coordinator_core.win_portability import no_console_creationflags

_WELL_KNOWN_WINDOWS_BASH = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def _resolve_posix_shell() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in _WELL_KNOWN_WINDOWS_BASH:
        if os.path.isfile(candidate):
            return candidate
    return None


POSIX_SHELL = _resolve_posix_shell()

requires_posix_shell = pytest.mark.skipif(
    POSIX_SHELL is None,
    reason="no POSIX shell resolvable -- the differential oracle cannot run the real command",
)


def run_real(cmd: str, cwd) -> tuple[int, str]:
    assert POSIX_SHELL is not None, "guarded by requires_posix_shell"
    import subprocess

    proc = subprocess.run(
        [POSIX_SHELL, "-c", cmd],
        cwd=str(cwd),
        capture_output=True,
        check=False, **no_console_creationflags(),
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="surrogateescape")
