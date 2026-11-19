"""Install-time launch probe for door images and the classifier that names a
Smart App Control (code-integrity) block apart from a missing image.

`door_install._resolve_launch_probe` consumes `probe_launch`; its contract is
`probe(image) -> (ok, detail)`, never raising on a blocked launch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional, Tuple

from coordinator_core.win_portability import no_console_creationflags

#: One-line diagnosis for a code-integrity block of an unsigned door image.
SAC_DIAGNOSIS = (
    "Smart App Control blocked the unsigned door image (WinError 4551, CodeIntegrity 3118); "
    "keep the .py forwarder route or turn Smart App Control off"
)

SAC_BLOCKED = "sac-blocked"
NOT_FOUND = "not-found"
OTHER = "other"

#: WinError codes meaning a policy refused the image: 4551 (Smart App Control),
#: 1260 (blocked by group policy).
_BLOCK_WINERRORS = frozenset({4551, 1260})
#: WinError codes meaning the file or its directory is absent.
_MISSING_WINERRORS = frozenset({2, 3})
#: Markers in an exception's text that name a code-integrity block outright.
_EXC_BLOCK_MARKERS = ("smart app control", "codeintegrity", "4551")
_BLOCK_MARKERS = (*_EXC_BLOCK_MARKERS, "permission denied", "blocked by")
_MISSING_MARKERS = ("not found", "no such file")

#: The cheapest no-op every door image answers without touching the engine.
_NOOP_ARGV = ("--help",)
_PROBE_TIMEOUT_S = 10.0

#: `runner(argv, timeout) -> (returncode, combined output)`; may raise OSError.
Runner = Callable[[list, float], Tuple[int, str]]


def classify_launch_failure(
    exc: Optional[BaseException] = None, rc: Optional[int] = None, output: str = ""
) -> str:
    """Classes a failed launch as `sac-blocked`, `not-found` or `other`.

    An `OSError` with winerror 4551/1260, or exit 126 whose output reads
    permission denied (Git Bash's face of the same block), is `sac-blocked`.
    A `FileNotFoundError`, winerror 2/3 or exit 127 is `not-found`."""
    text = (output or "").lower()
    if exc is not None:
        winerror = getattr(exc, "winerror", None)
        if winerror in _BLOCK_WINERRORS:
            return SAC_BLOCKED
        if isinstance(exc, FileNotFoundError) or winerror in _MISSING_WINERRORS:
            return NOT_FOUND
        if any(marker in str(exc).lower() for marker in _EXC_BLOCK_MARKERS):
            return SAC_BLOCKED
        return OTHER
    if rc == 126 and any(marker in text for marker in _BLOCK_MARKERS):
        return SAC_BLOCKED
    if rc == 127 or (rc not in (None, 0) and any(marker in text for marker in _MISSING_MARKERS)):
        return NOT_FOUND
    return OTHER


def _default_runner(argv: list, timeout: float) -> Tuple[int, str]:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        **no_console_creationflags(),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def probe_launch(image: Path, *, runner: Optional[Runner] = None) -> Tuple[bool, str]:
    """Spawns `image --help` once, bounded, without a console window.

    Returns `(True, detail)` when the image started (any exit status other
    than a launch failure), else `(False, detail)`; a SAC block carries
    `SAC_DIAGNOSIS`. Never raises."""
    run = runner or _default_runner
    argv = [str(image), *_NOOP_ARGV]
    try:
        rc, output = run(argv, _PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"{Path(image).name} did not answer within {_PROBE_TIMEOUT_S:.0f}s"
    except Exception as exc:  # any launch fault is a verdict, never an install abort
        kind = classify_launch_failure(exc=exc)
        if kind == SAC_BLOCKED:
            return False, f"{SAC_DIAGNOSIS}: {exc}"
        return False, f"{kind}: {exc!r}"
    if rc in (126, 127):
        kind = classify_launch_failure(rc=rc, output=output)
        if kind == SAC_BLOCKED:
            return False, f"{SAC_DIAGNOSIS}: exit {rc}"
        if kind == NOT_FOUND:
            return False, f"not-found: exit {rc}"
        return False, f"other: exit {rc} {output.strip()[:200]}"
    return True, f"launched (exit {rc})"
