"""
coordinator_core.pyresolve -- portable Python interpreter resolver, the naked-Python
port of ``coordinator/lib/resolve-python.sh`` (bash, [DoE-claude] repo).

Byte-parity target: ``coordinator/lib/resolve-python.sh``. Behaviors ported
byte-identically per the T4c build recipe: pin precedence (``COORDINATOR_PYTHON`` env
var beats ``machine-local get coordinator.python`` beats OS-detect fallback), the
"found-but-broken pin is a hard failure, never a silent fallthrough" rule, the Windows
windowless-preferred probe order, the WindowsApps/Store-Python rejection, and the
PATH-seeding side effect for child re-resolution (carried into CLI mode only -- an
importing Python caller already knows its own interpreter via ``sys.executable`` and
needs no PATH mutation for itself).

Two consumers:
    1. In-process import (``resolve_python_bin()``) -- for any claude-klabauter Python module
       that itself needs to know the pinned/detected interpreter without a subprocess
       hop (rare; ``sys.executable`` already answers "what am I running under" for
       the overwhelmingly common case).
    2. Cold CLI mode (``python3 -m coordinator_core.pyresolve --print-bin``) -- for
       bootstrap-tier bash scripts that still need full pin-fidelity (env/machine-local
       precedence, OS-detect fallback) but have themselves stayed bash. Prints
       ``PYTHON_BIN\\tPYTHON_ARGS_JOINED`` on one line to stdout.

No brew-path logic: the bash oracle's non-Windows branch is a bare ``python3`` ->
``python`` PATH probe -- there is no Homebrew-prefix resolution branch on disk to port
(verified by a case-insensitive ``grep brew`` of the 323-line source finding zero
matches; recipe-T4c-resolve-python-bootstrap-cohort.md § 1).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4c;
recipe-T4c-resolve-python-bootstrap-cohort.md (DoE-claude
scratch/subagent-sandbox/bash-to-python-engine-migration/).
"""

from __future__ import annotations

import argparse
import ntpath
import os
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core._claude_klabauter_root import (
    _machine_local_get,
    clear_machine_local_cache,
)
from coordinator_core.win_portability import is_executable

_WINDOWS_PYORG_VERSIONS = ("313", "312", "311", "310")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Windowless (``/SUBSYSTEM:WINDOWS``) interpreter basenames -- a general-purpose
_WINDOWLESS_BASENAMES = ("pythonw.exe", "pyw.exe")


def _console_sibling(windowless_path: str) -> str:
    # `windowless_path` is a WINDOWS-shaped path string (baked/resolved for a
    directory = ntpath.dirname(windowless_path)
    basename = ntpath.basename(windowless_path).lower()
    if basename == "pythonw.exe":
        sibling = ntpath.join(directory, "python.exe")
    elif basename == "pyw.exe":
        sibling = ntpath.join(directory, "py.exe")
    else:
        return ""
    return sibling if os.path.isfile(sibling) else ""


_validate_cache: Dict[str, bool] = {}


def clear_resolution_cache() -> None:
    """Reset both memoization caches -- the pin-validation cache here, and
    the shared ``_machine_local_get`` cache in
    ``coordinator_core._claude_klabauter_root``. Tests that mutate
    ``COORDINATOR_PYTHON``, ``MACHINE_LOCAL_IMPL``, or ``CLAUDE_HOME`` between
    cases MUST call this in setup/teardown -- a stale entry would otherwise
    return a memoized result for a since-changed steering env, and the module
    has no other reset hook."""
    _validate_cache.clear()
    clear_machine_local_cache()


class PythonPinInvalid(RuntimeError):
    pass


def _validate_interpreter(path: str) -> bool:
    """True iff ``path -c 'import sys'`` succeeds -- the bash resolver's pin-validation
    probe, ported verbatim.

    Memoized per process, keyed by ``path`` alone: validity is a property of the
    binary the path points at, not of which env tier (``COORDINATOR_PYTHON`` vs.
    machine-local) supplied that path -- two different steering envs that happen
    to resolve to the same path get the same (correct) answer from one spawn.
    A ``False`` result is cached too, deliberately: ``resolve_python_bin`` turns a
    ``False`` here into ``PythonPinInvalid`` on every call regardless, so memoizing
    it does not convert a hard failure into a silent success -- it just avoids
    re-spawning to re-derive the same hard failure.

    Skips the spawn entirely when ``path`` IS ``sys.executable``: we are
    demonstrably running inside that interpreter right now, which is a
    strictly stronger proof than a successful ``-c 'import sys'`` probe of a
    separate process could ever provide. A strict subset of the existing
    contract, not a widened one -- every other path still probes exactly as
    before."""
    if path == sys.executable:
        return True
    if path in _validate_cache:
        return _validate_cache[path]
    try:
        result = subprocess.run(
            [path, "-c", "import sys"],
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError:
        _validate_cache[path] = False
        return False
    valid = result.returncode == 0
    _validate_cache[path] = valid
    return valid


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform == "cygwin"


def _is_store_python(path: str) -> bool:
    return "windowsapps" in path.lower()


def _pyorg_search(prefer_windowless: bool) -> Optional[str]:
    candidates: List[str] = []

    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        programs_python = os.path.join(localappdata, "Programs", "Python")
        if os.path.isdir(programs_python):
            children = [
                os.path.join(programs_python, name)
                for name in os.listdir(programs_python)
                if name.startswith("Python3")
            ]
            candidates.extend(sorted(children, reverse=True))

    for ver in _WINDOWS_PYORG_VERSIONS:
        candidates.append(os.path.join("C:\\Program Files", f"Python{ver}"))
    for ver in _WINDOWS_PYORG_VERSIONS:
        candidates.append(os.path.join("C:\\", f"Python{ver}"))

    probes = ("pythonw.exe", "python.exe") if prefer_windowless else ("python.exe", "pythonw.exe")

    for directory in candidates:
        for exe in probes:
            hit = os.path.join(directory, exe)
            if os.path.isfile(hit) and is_executable(hit):
                return hit
    return None


def _which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)


def _launcher_available(cmd: str) -> bool:
    try:
        result = subprocess.run(
            [cmd, "-3", "--version"],
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError:
        return False
    return result.returncode == 0


def _resolve_windows(prefer_windowless: bool) -> Tuple[str, List[str]]:
    hit = _pyorg_search(prefer_windowless=prefer_windowless)
    if hit:
        return hit, []

    names = ("pythonw", "python3", "python") if prefer_windowless else ("python3", "python", "pythonw")
    for name in names:
        candidate = _which(name)
        if candidate and not _is_store_python(candidate):
            return candidate, []

    if _launcher_available("pyw"):
        return "pyw", ["-3"]
    if _launcher_available("py"):
        return "py", ["-3"]

    return "", []


def _resolve_non_windows() -> Tuple[str, List[str]]:
    for name in ("python3", "python"):
        candidate = _which(name)
        if candidate:
            return candidate, []
    return "", []


def resolve_python_bin(prefer_windowless: bool = True) -> Tuple[str, List[str]]:
    """Resolve the pinned/detected Python interpreter.

    Resolution order (mirrors ``resolve-python.sh``):
        1. ``COORDINATOR_PYTHON`` env var (non-empty).
        2. ``machine-local get coordinator.python``.
        3. OS-detect fallback (Windows python.org probe / PATH / py launcher;
           non-Windows ``python3`` -> ``python`` PATH probe).

    A found pin (tier 1 or 2) that fails ``-c 'import sys'`` validation raises
    ``PythonPinInvalid`` -- never silently falls through to tier 3.

    ``prefer_windowless`` (Windows OS-detect tier only, default ``True`` --
    preserves prior behavior for every existing caller): whether tier 3 probes for
    ``pythonw.exe``/``pyw.exe`` before the console ``python.exe``/``py``. The
    windowless preference exists to suppress a console-window flash on hook
    invocations whose caller controls stdin (heredoc, ``/dev/null``, args-only) --
    see the WARNING block in ``coordinator/lib/resolve-python.sh``. It is NOT safe
    for a general-purpose interpreter a caller may hand a live stdin pipe: pythonw
    is ``/SUBSYSTEM:WINDOWS`` and the child can receive a null/invalid stdin handle
    from a console-less parent. Callers baking a general-purpose shim (e.g.
    ``python3.cmd``) must pass ``prefer_windowless=False``.

    Returns ``(python_bin, python_args)``; ``python_bin == ""`` means no interpreter
    was found anywhere. Does NOT mutate ``PATH`` -- that side effect is CLI-mode-only
    (see ``main()``), since an importing Python caller already knows its own
    interpreter via ``sys.executable`` and has no PATH-reresolution need of its own.
    """
    pin_candidate = os.environ.get("COORDINATOR_PYTHON", "").strip()
    if not pin_candidate:
        ml_pin = _machine_local_get("coordinator.python")
        if ml_pin:
            pin_candidate = ml_pin

    if pin_candidate:
        if _validate_interpreter(pin_candidate):
            return pin_candidate, []
        raise PythonPinInvalid(
            f"resolve-python: pinned interpreter '{pin_candidate}' is invalid "
            "(exists but cannot run 'import sys'). Run bin/ensure-coordinator-venv.sh "
            "to rebuild the coordinator venv."
        )

    if _is_windows():
        return _resolve_windows(prefer_windowless)
    return _resolve_non_windows()


def resolve_machine_python_bin(prefer_windowless: bool = True) -> Tuple[str, List[str]]:
    """Resolve the OS-detect ("machine") tier ONLY -- deliberately skipping
    both pin tiers (``COORDINATOR_PYTHON`` env var, ``machine-local get
    coordinator.python``) that ``resolve_python_bin()`` checks first.

    This exists as PUBLIC API, not as an inlined call to the private
    ``_resolve_windows``/``_resolve_non_windows`` helpers, because the
    coordinator venv the pin tiers can resolve to is a SHARED MUTABLE TREE
    rebuilt under live sessions (50-70 concurrent on this machine) -- a
    caller resolving a hook command's interpreter must never resolve through
    that pin, on pain of baking a path that can vanish mid-rebuild out from
    under it. Do NOT "simplify" this back into a call to
    ``resolve_python_bin()`` or back into direct private-helper access: both
    reintroduce the coupling this function exists to remove. See
    ``docs/plans/2026-08-14-the-venv-fallback-stops-being-something.md`` C2.

    ``prefer_windowless`` -- same parameter, same semantics, as
    ``resolve_python_bin()``'s (see its docstring); this is the Windows
    OS-detect probe order knob, meaningless on non-Windows (``_resolve_non_windows``
    takes no such parameter). On non-Windows the value is silently dropped --
    deliberate, mirroring ``resolve_python_bin()``'s own contract at this same
    point, not an oversight.

    Returns ``(python_bin, python_args)``; ``python_bin == ""`` means no
    interpreter was found. Does NOT mutate ``PATH`` -- same contract as
    ``resolve_python_bin()``.
    """
    if _is_windows():
        return _resolve_windows(prefer_windowless=prefer_windowless)
    return _resolve_non_windows()


def _prepend_path(bin_path: str) -> None:
    if not bin_path or bin_path in ("py", "pyw"):
        return
    bin_dir = os.path.dirname(bin_path)
    if not bin_dir:
        return
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in path_parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m coordinator_core.pyresolve",
        description="Resolve the pinned/detected Python interpreter for cold bash callers.",
    )
    parser.add_argument(
        "--print-bin",
        action="store_true",
        help="Print 'PYTHON_BIN\\tPYTHON_ARGS_JOINED' for the resolved interpreter and exit 0.",
    )
    args = parser.parse_args(argv)

    if not args.print_bin:
        parser.print_help(sys.stderr)
        return 2

    try:
        python_bin, python_args = resolve_python_bin()
    except PythonPinInvalid as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _prepend_path(python_bin)

    print(f"{python_bin}\t{' '.join(python_args)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
