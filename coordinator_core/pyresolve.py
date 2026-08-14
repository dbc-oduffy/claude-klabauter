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
import os
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.win_portability import is_executable

_WINDOWS_PYORG_VERSIONS = ("313", "312", "311", "310")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# per-process memoization -- see resolve_python_bin's "highest per-invocation
# win" spawn-census finding (state/audits/2026-08-06-repo-root-and-git-wrapper-
# hitlist.md): both the pin-validation probe and the machine-local shell-out
# are genuine subprocess isolation boundaries (not convertible in-process, see
# module docstring), so the fix is memoizing the *result* per process rather
# than converting the call. Caches persist for the life of the importing
# process (spawn-per-call model means that's one op invocation) and must be
# cleared explicitly between test cases via clear_resolution_cache().
# ---------------------------------------------------------------------------

_validate_cache: Dict[str, bool] = {}
_machine_local_cache: Dict[Tuple[str, str], Optional[str]] = {}


def clear_resolution_cache() -> None:
    """Reset both memoization caches. Tests that mutate ``COORDINATOR_PYTHON``,
    ``MACHINE_LOCAL_IMPL``, or ``CLAUDE_HOME`` between cases MUST call this in
    setup/teardown -- a stale entry would otherwise return a memoized result
    for a since-changed steering env, and the module has no other reset
    hook."""
    _validate_cache.clear()
    _machine_local_cache.clear()


class PythonPinInvalid(RuntimeError):
    """Raised when a pinned interpreter (env var or machine-local) exists on disk but
    fails ``-c 'import sys'`` validation -- e.g. a dangling venv shim. Mirrors the bash
    resolver's hard-failure contract: a broken pin must never silently fall through to
    the OS-detect tier."""


# ---------------------------------------------------------------------------
# machine-local helpers (mirrors coordinator_core.ops.queue_append's pattern)
# ---------------------------------------------------------------------------


def _claude_home() -> str:
    override = os.environ.get("CLAUDE_HOME")
    if override:
        return override
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".claude")


def _machine_local_impl() -> str:
    """Return the path to ``_machine_local.py``, honouring ``MACHINE_LOCAL_IMPL`` for
    tests."""
    override = os.environ.get("MACHINE_LOCAL_IMPL")
    if override:
        return override
    settings_home_impl = os.path.join(str(settings_home()), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl
    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``_machine_local.py get <key>`` and return the value, or None on any
    failure (missing impl, nonzero exit, empty stdout) -- mirrors the bash resolver's
    "machine-local unavailable/empty -> fall through" contract.

    Memoized per process, keyed by ``(key, resolved impl path)`` -- the impl path
    already folds in every env var that steers *which* ``_machine_local.py`` gets
    shelled out to (``MACHINE_LOCAL_IMPL``, ``CLAUDE_HOME`` via
    ``_machine_local_impl()``/``settings_home()``), so a changed override env
    naturally produces a different cache key rather than a stale hit. A None
    (unavailable/empty) result is memoized too: it is a deterministic function of
    the impl's on-disk state for the remainder of this process, not a transient
    failure that later calls should retry."""
    impl = _machine_local_impl()
    cache_key = (key, impl)
    if cache_key in _machine_local_cache:
        return _machine_local_cache[cache_key]

    if not os.path.exists(impl):
        _machine_local_cache[cache_key] = None
        return None
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError:
        _machine_local_cache[cache_key] = None
        return None
    if result.returncode != 0 or not result.stdout.strip():
        _machine_local_cache[cache_key] = None
        return None
    value = result.stdout.strip()
    _machine_local_cache[cache_key] = value
    return value


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
    re-spawning to re-derive the same hard failure."""
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


# ---------------------------------------------------------------------------
# Windows OS-detect tier
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform == "cygwin"


def _is_store_python(path: str) -> bool:
    """Reject the Microsoft Store Python install -- sandboxed, inconsistent
    permissions; python.org installs are always preferred. Ported verbatim from
    ``_resolve_python_is_store``."""
    return "windowsapps" in path.lower()


def _pyorg_search(prefer_windowless: bool) -> Optional[str]:
    """Probe known python.org install locations in descending version order, looking
    for a windowless interpreter first (console-flash suppression) then the console
    binary. Returns the first absolute path found, or None. Ported from
    ``_resolve_python_pyorg_search``; the Windows-native path handling in Python needs
    no cygpath translation the bash oracle required."""
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
            # sort -r gives descending lex order; matches the bash oracle exactly
            # (including its documented 3.9-vs-3.10 lex-sort limitation -- not fixed
            # here for byte-parity with the resolution corpus).
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
    # Step 1 -- direct probe of python.org installer locations, bypassing PATH (and
    # therefore the Store-Python symlink farm).
    hit = _pyorg_search(prefer_windowless=prefer_windowless)
    if hit:
        return hit, []

    # Step 2 -- PATH resolution, rejecting any match under WindowsApps. Probe order
    # mirrors the windowless preference so a general-purpose (console-wanting) caller
    # doesn't get handed a *w.exe binary here even though pyorg_search above already
    # honored prefer_windowless.
    names = ("pythonw", "python3", "python") if prefer_windowless else ("python3", "python", "pythonw")
    for name in names:
        candidate = _which(name)
        if candidate and not _is_store_python(candidate):
            return candidate, []

    # Step 3 -- the py / pyw launcher (PEP 514 registry-backed selection).
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


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


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


def _prepend_path(bin_path: str) -> None:
    """Prepend ``bin_path``'s directory to this process's ``PATH`` so any child this
    process spawns by bare interpreter name (rare in CLI mode, but mirrors the bash
    oracle's side effect for parity) resolves the same install. Skipped for the py/pyw
    launcher, which resolves via the Windows registry (PEP 514), not PATH -- ported
    verbatim from the bash guard.

    Unguarded ``os.environ`` write, deliberately: reachable only from this module's
    own ``main()`` under its ``__main__`` guard, itself only ever ``subprocess``-spawned
    (never imported and called in-process) -- under the DR-215 spawn-per-call model the
    process that makes this write exits immediately after, same as the bash `export` it
    ports. No env_overlay wrap needed; see the wider unguarded-write review in
    state/review-trail/findings/2026-07-22-codereview-sliceenv-hygiene-cluster-... .md
    (Finding 4)."""
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

    # PATH-seeding side effect -- CLI-mode-only per module docstring.
    _prepend_path(python_bin)

    print(f"{python_bin}\t{' '.join(python_args)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
