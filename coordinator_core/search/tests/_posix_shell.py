"""Resolve the POSIX shell a differential test must run its REAL command through.

Every differential test in this package compares our in-process answer against what the
command the agent actually typed would have printed. That oracle is only as good as the
shell it runs the command in, and on Windows the obvious spelling is silently wrong:
``subprocess.run(cmd, shell=True)`` resolves to ``cmd.exe``, which has no ``grep``, no
``sed``, no ``cat``, and no ``ls``. Every case then returns rc=1 with empty stdout, so a
differential assertion compares our correct answer against ``[]`` and fails for a reason
that has nothing to do with the answer.

Negative spec -- what this module must NOT do:

- **Never fall back to ``shell=True``.** A ``cmd.exe`` oracle does not produce a weaker
  verdict, it produces a WRONG one, in whichever direction the comparison happens to land.
- **Never let an unresolvable shell read as a pass.** A differential test with no working
  oracle must skip, so the absence of a verdict is visible as an absence. Callers use
  :data:`requires_posix_shell` for that.
- **Never resolve ``sh``.** The harness spawns ``bash``; a differential oracle that runs a
  different shell is measuring a different machine than the one the guard sits in front of.
"""

from __future__ import annotations

import os
import shutil

import pytest

#: Git-for-Windows install locations, probed only after PATH resolution fails. `bash` is
#: reliably on PATH under the harness's own Bash tool but NOT under a PowerShell-hosted
#: pytest run, which is how this package's tests are usually invoked on this box -- so
#: PATH-only resolution skips the entire differential corpus on the primary platform.
_WELL_KNOWN_WINDOWS_BASH = (
    r"C:\Program Files\Git\bin\bash.exe",  # abs-path-ok: Git-for-Windows default install prefix, not a citation of this box
    r"C:\Program Files\Git\usr\bin\bash.exe",  # abs-path-ok: same, MSYS2 sibling location
)


def _resolve_posix_shell() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in _WELL_KNOWN_WINDOWS_BASH:
        if os.path.isfile(candidate):
            return candidate
    return None


#: Absolute path to a POSIX shell, or None when this box has none.
POSIX_SHELL = _resolve_posix_shell()

#: Skip-marker for any test whose verdict depends on running the real command.
requires_posix_shell = pytest.mark.skipif(
    POSIX_SHELL is None,
    reason="no POSIX shell resolvable -- the differential oracle cannot run the real command",
)


def run_real(cmd: str, cwd) -> tuple[int, str]:
    """Run ``cmd`` through the resolved POSIX shell, returning (returncode, raw stdout).

    Returns stdout as RAW TEXT, never a line list: the read shapes this package serves are
    differentiated by exactly the bytes a line-splitting round-trip erases -- a missing
    final newline, a CRLF, an empty body, a form feed. Callers that genuinely want lines
    split them themselves, at which point that normalisation is a visible choice in the
    test rather than an invisible one in the helper.
    """
    assert POSIX_SHELL is not None, "guarded by requires_posix_shell"
    import subprocess

    proc = subprocess.run(
        [POSIX_SHELL, "-c", cmd],
        cwd=str(cwd),
        capture_output=True,
        # Bytes, not text: `text=True` applies universal-newline translation, which turns
        # a CRLF divergence into a silent agreement. AC6 exists to catch exactly that.
        check=False,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="surrogateescape")
