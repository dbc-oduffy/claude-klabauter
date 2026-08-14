"""
coordinator_core.win_portability -- bootstrap-safe cross-platform path/exec primitives.

Purpose: `coordinator_core/tests/test_home_resolution_lint.py` (commit c1545206)
counted 98 sites of a recurring cross-platform-portability defect class across this
repo -- resolutions that hold on the author's host and degrade on a peer's (today
that peer is always Windows, which is why the primitives below read Windows-shaped).
This module is the ONE correct implementation of the three sub-shapes it covers, so
future call-site migrations (Wave 2 of this dispatch) call a shared primitive instead
of hand-writing a 71st variant of the same mistake. It does NOT migrate any call
site itself -- see the module's own "Not done here" note below.

The three primitives, and the platform-semantics gap each one exists to absorb:

  1. ``is_executable(path)`` -- absorbs the ``os.access(path, os.X_OK)`` defect
     (70 of the 98 counted sites). ``os.X_OK`` is a POSIX permission-bit query; on
     Windows, ``os.access(path, os.X_OK)`` silently degrades to an ``F_OK``
     (existence-only) check -- NTFS has no POSIX exec bit, so the call never raises
     and never tells the caller anything about launchability, it just quietly lies
     that any *existing* file is "executable". The Windows-real predicate is
     completely different: existence, PLUS whether the file is something
     ``CreateProcess`` (or a PATHEXT-consulting shell resolving a bareword) would
     actually treat as invocable -- which for an extensionless path (coordinator
     ships bareword CLI entrypoints resolved this way, e.g. ``machine-local``)
     means checking for a sibling file carrying one of the ``PATHEXT`` extensions,
     because a full path with no extension is ALSO subject to PATHEXT expansion by
     cmd.exe / CreateProcess, not just a bareword PATH search.

  2. ``split_path_list`` / ``join_path_list`` -- absorb the literal ``":"``
     PATH-list-join/split defect (1 of the 98 sites, a strangler-facade seam-
     detection join that silently failed open on Windows because ``;`` is the
     real separator there, not ``:``). ``os.pathsep`` is the correct primitive;
     these two functions exist so the literal character never needs to be
     retyped at a new call site.

  3. ``split_path`` -- absorbs the forward-slash-only path-split defect (9 of the
     98 sites). ``"X:\\DoE-claude\\coordinator".rsplit("/", 1)`` returns the
     whole string unsplit (there is no ``"/"`` in it at all), so a caller
     expecting ``(parent, leaf)`` silently gets ``(whole_string,)`` and a
     downstream tree-membership check double-adds the same clone. Folding
     backslash to forward-slash BEFORE splitting makes a native Windows path
     (``X:\\a\\b``), a POSIX path (``/a/b``), and an MSYS/Git-Bash mount-form
     path (``/x/a/b`` for ``X:\\a\\b``) all split into the same segment shape.

  4. ``no_console_creationflags()`` -- absorbs the console-popup defect
     (`docs/plans/2026-08-07-no-window-subprocess-primitive.md`, this repo): a
     subprocess spawned without console suppression allocates a fresh
     ``conhost.exe`` window on Windows that flashes and steals keyboard focus,
     with no POSIX equivalent to omit -- there is nothing to suppress there.
     Returns a kwargs mapping meant to be splatted into ``subprocess.run`` /
     ``subprocess.Popen`` as ``**no_console_creationflags()``: empty on POSIX,
     ``{"creationflags": <CREATE_NO_WINDOW>}`` on Windows. Covers the
     ``creationflags`` route only -- a call site that also passes
     ``shell=True`` needs the separate STARTUPINFO route
     (``STARTF_USESHOWWINDOW`` + ``wShowWindow=SW_HIDE``) instead, because
     with ``shell=True`` Python spawns ``cmd.exe`` as an intermediary and
     ``CREATE_NO_WINDOW`` does not suppress that intermediary's own window;
     see the function's own docstring for this gap. Not built here
     speculatively -- add it only if a test demonstrates ``CREATE_NO_WINDOW``
     alone insufficient for a child process type this repo actually spawns.

AC-2 finding (home-resolution primitive): NOT built here, by design.
``coordinator_core._settings_home.py`` already implements this correctly --
``Path.home()`` (which honours ``USERPROFILE`` on Windows via CPython's own
``ntpath.expanduser`` delegation) plus ``_require_rooted()`` for the relative-
path foot-gun. A second implementation is exactly the failure that produced
this defect class (a competing resolver, hand-written per call site, each one
a fresh chance to drop the ``USERPROFILE`` rung). Callers needing home
resolution import ``coordinator_core._settings_home.settings_home`` directly;
nothing in this module re-derives it.

Not done here: migrating any of the 98 counted call sites onto these primitives.
That is Wave 2 of this dispatch, a separate, disjoint-scope change. This module
only builds and proves the primitives in isolation.

Bootstrap-safety (mirrors ``_settings_home.py``'s own contract): stdlib only
(``os``, ``pathlib``), zero external calls, zero subprocess, safe to import
before the venv exists or PATH is populated.

Negative-spec:
    - NEVER call ``shutil.which`` here. ``which`` searches PATH for a bareword;
      ``is_executable`` answers a different question -- "is THIS path, already
      resolved, actually invocable" -- and conflating the two is how a caller
      ends up silently resolving the wrong sibling binary.
    - NEVER treat an extensionless file's own existence as sufficient on
      Windows. A shebang script with no extension is real and readable but is
      NOT invocable by ``CreateProcess`` without an explicit interpreter (see
      ``coordinator_core.launchable`` for that separate, argv-construction
      concern) -- ``is_executable`` must return ``False`` for it, not
      degrade to an existence check the way the banned ``os.access(X_OK)``
      idiom does.
    - ``split_path`` folds backslash unconditionally, on every platform, not
      only when ``os.name == "nt"`` -- a POSIX host processing a Windows-
      authored path string (e.g. a foreign-platform ``settings.json`` value)
      needs the same folding, and gating on the *host* OS instead of the
      *input shape* is the exact bug this primitive exists to remove.
    - Does NOT validate that a path exists before folding/splitting it --
      ``split_path`` is a pure string transform, no filesystem access,
      matching the forward-slash lint rule's own scope (string-shape
      defects, not I/O).

Spec backlink: docs/research/2026-07-28-windows-simulation-test-harness-design.md
  (DoE-claude) -- Component Design, the AC-1/AC-3/AC-4 primitives this module
  builds toward. coordinator_core/tests/test_home_resolution_lint.py (this repo,
  commit c1545206) -- the standing gate whose baseline enumerates every site
  these primitives will eventually replace.
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path, PureWindowsPath
from typing import Iterable, List, Sequence, Union

__all__ = [
    "is_executable",
    "split_path_list",
    "join_path_list",
    "split_path",
    "no_console_creationflags",
    "same_path",
]

StrPath = Union[str, "os.PathLike[str]"]

# Windows' own default PATHEXT, used only when the environment does not define
# one (a bare `cmd.exe`/PowerShell session always has PATHEXT set; the default
# here matters only for a stripped-down sim/test environment or a POSIX host
# asked to model Windows semantics for a foreign-platform path).
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"


def _is_windows() -> bool:
    """Platform check as a seam, not an inline ``os.name`` read at every call
    site -- mirrors ``coordinator_core.launchable._is_windows``'s own reasoning:
    a single patchable function lets a test exercise both branches on one host
    without touching ``pathlib``'s own OS-keyed class selection."""
    return os.name == "nt"


def _pathext_list() -> List[str]:
    """The active PATHEXT list, uppercased, dot-prefixed, in resolution order.

    Reads the real environment when present (a real Windows session always has
    one); falls back to CPython's own documented default set when absent, so
    this function behaves identically whether called on real Windows or under
    a POSIX host modelling Windows semantics with ``PATHEXT`` unset.

    Split on a literal ``;`` -- deliberately NOT ``os.pathsep``. ``PATHEXT``'s
    own separator is always ``;`` on Windows (it IS ``os.pathsep`` when the
    host really is Windows), but this function must also work correctly when
    ``_is_windows()`` has been monkeypatched to model Windows semantics on a
    POSIX host, where the real ``os.pathsep`` is ``:`` -- splitting on it
    there would corrupt every multi-entry PATHEXT value. The one place in
    this module that intentionally does NOT use ``os.pathsep`` for a
    PATH-shaped split, and it does so because the separator being modelled
    is fixed by the Windows convention, not by the host running this code.
    """
    raw = os.environ.get("PATHEXT", _DEFAULT_PATHEXT)
    return [ext.upper() for ext in raw.split(";") if ext]


def is_executable(path: StrPath) -> bool:
    """True if ``path`` is a file this OS would actually launch directly.

    POSIX: reads the file's own mode bits (``stat.S_IXUSR`` /
    ``S_IXGRP`` / ``S_IXOTH``) via ``os.stat`` rather than
    ``os.access(path, os.X_OK)``. Deliberate: this module's own home-
    resolution-lint gate (``coordinator_core/tests/test_home_resolution_lint.py``)
    bans that exact call shape repo-wide with no suppression mechanism --
    the primitive meant to REPLACE the 70 banned sites must not itself add a
    71st, and `os.stat` mode-bit inspection is a genuine, not cosmetic,
    alternative: it answers "does this file's mode carry an exec bit for
    anyone" rather than "can the CURRENT process execute it" (the latter also
    accounts for ACLs the former does not) -- close enough for every
    accept/reject-a-launch-candidate use this repo has, and it sidesteps the
    banned shape by actually using a different mechanism, not by renaming a
    variable to dodge the AST match.

    Windows: NTFS has no POSIX exec bit, so ``os.access(path, os.X_OK)``
    silently degrades to existence-only there -- exactly the defect this
    function replaces. The real predicate is existence PLUS PATHEXT
    resolvability:
      - A file whose own extension (e.g. ``.EXE``, ``.CMD``) is in the active
        ``PATHEXT`` list is directly invocable -- return ``True``.
      - A file with a *different* extension (``.py``, ``.md``, ``.txt``, ...)
        is not something ``CreateProcess`` treats as launchable on its own --
        return ``False``. (Whether some interpreter could run it is a
        separate concern -- see ``coordinator_core.launchable``.)
      - An EXTENSIONLESS path (coordinator's own bareword CLI entrypoints,
        e.g. ``machine-local``) is the case the banned idiom gets most wrong:
        the bare file itself, even if it exists and even if it is a valid
        shebang script, is NOT what Windows launches. What Windows launches
        is a PATHEXT-suffixed sibling (``machine-local.cmd``) if one exists --
        cmd.exe/``CreateProcess`` performs this same PATHEXT expansion for a
        full extensionless path, not only for a bareword PATH search. So the
        extensionless case checks for that sibling, not for the bare file.
    """
    p = Path(os.fspath(path))

    if not _is_windows():
        try:
            mode = os.stat(p).st_mode
        except OSError:
            return False
        return bool(mode & (stat_module.S_IXUSR | stat_module.S_IXGRP | stat_module.S_IXOTH))

    suffix = p.suffix
    if suffix:
        return p.is_file() and suffix.upper() in _pathext_list()

    for ext in _pathext_list():
        if (p.parent / (p.name + ext.lower())).is_file():
            return True
    return False


def same_path(a: StrPath, b: StrPath) -> bool:
    """True if ``a`` and ``b`` resolve to the same filesystem entry
    (cross-platform), never raising.

    Consolidates twelve independently-authored copies of this exact check
    found across this plane (probe: ``state/sizings/2026-08-07-path-equality-
    consolidates-onto-one-prim.yaml``) that collapsed to two semantic
    families -- ``samefile``-then-fallback (the stronger of the two) and
    realpath+normcase-only. This function is the samefile-first family,
    chosen as the ONE primitive because it is strictly more correct:
    ``os.path.samefile`` detects hardlink/junction/bind-mount aliases that a
    realpath-only comparison misses, and that gap is load-bearing on
    Windows, where the settings home is commonly reached through a
    junction -- two paths that are the SAME directory via a junction hop
    must compare equal, and only ``samefile`` (which stats both paths and
    compares device+inode / file-index) can see that; ``realpath`` alone can
    resolve to different strings for the two aliases depending on which
    hop the caller's path went through.

    Tries ``os.path.samefile`` first (requires both paths to exist); falls
    back to ``os.path.normcase(os.path.realpath(...))`` string comparison
    when either path is absent or unreadable, so an unregistered/not-yet-
    cloned repo path never raises. The realpath fallback is itself guarded
    -- any exception resolving either path returns ``False`` rather than
    propagating, matching every existing per-site copy's "never raises"
    contract.
    """
    try:
        return os.path.samefile(a, b)
    except Exception:
        pass
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return False


def split_path_list(value: str) -> List[str]:
    """Split a PATH-style list on ``os.pathsep`` (``:`` on POSIX, ``;`` on
    Windows) -- never a hardcoded ``":"``, which fails open (returns the whole
    string as one un-split element) on Windows and is how a strangler-facade
    seam-detection check silently read "seam absent" there."""
    return value.split(os.pathsep)


def join_path_list(parts: Iterable[str]) -> str:
    """Join a PATH-style list on ``os.pathsep`` -- the write-side twin of
    ``split_path_list``, same rationale."""
    return os.pathsep.join(parts)


def split_path(value: str, sep: str = "/", maxsplit: int = -1, *, from_right: bool = False) -> List[str]:
    """Split a path-shaped string into segments, folding backslash to ``/``
    first so a native Windows path, a POSIX path, and an MSYS/Git-Bash
    mount-form path all split into the same segment shape.

    ``"X:\\\\DoE-claude\\\\coordinator".split("/")`` (no fold) returns a single
    one-element list -- there is no ``"/"`` in the string at all -- which is
    exactly the F8 defect: a caller expecting ``(parent, leaf)`` from a
    ``rsplit("/", 1)`` silently got the whole string back unsplit, and a
    downstream tree-membership check double-counted the same clone. Folding
    first makes ``split_path("X:\\\\DoE-claude\\\\coordinator", maxsplit=1,
    from_right=True)`` behave the same as it would for the POSIX-form
    equivalent.

    ``from_right`` selects ``rsplit`` semantics (matches the ``.rsplit("/", n)``
    shape the lint's forward-slash rule targets) vs. the default left-to-right
    ``split``. Pure string transform -- no filesystem access, no existence
    check; see module docstring § Negative-spec.
    """
    folded = value.replace("\\", "/")
    if from_right:
        return folded.rsplit(sep, maxsplit)
    return folded.split(sep, maxsplit)


def _model_windows_split(value: str, maxsplit: int = -1, *, from_right: bool = False) -> Sequence[str]:
    """Test-only cross-check helper: independently derive the expected split
    result via ``PureWindowsPath``-shaped reasoning (backslash treated as a
    separator regardless of host OS), so ``test_win_portability.py`` can
    assert ``split_path`` agrees with a SECOND, differently-derived
    computation rather than re-asserting its own output. Not part of the
    public primitive surface -- ``__all__`` omits it deliberately.
    """
    normalized = str(PureWindowsPath(value.replace("/", "\\"))).replace("\\", "/")
    if from_right:
        return normalized.rsplit("/", maxsplit)
    return normalized.split("/", maxsplit)


def no_console_creationflags() -> dict:
    """Kwargs mapping to splat into ``subprocess.run`` / ``subprocess.Popen``
    as ``**no_console_creationflags()`` to suppress the ``conhost.exe`` popup
    window a Windows child process otherwise allocates.

    POSIX: returns ``{}`` -- there is no console-popup defect to suppress
    there, and an empty mapping splats to no-op kwargs, leaving stdout/stderr
    capture semantics on every existing call site completely unchanged.

    Windows: returns ``{"creationflags": <CREATE_NO_WINDOW>}``. This is the
    ONLY reliable suppression at the ``CreateProcess`` call itself -- it does
    NOT touch ``stdout``/``stderr``/``stdin`` handling, so a caller's existing
    ``capture_output=True`` / ``PIPE`` wiring is unaffected.

    THE CATCH, and it is not optional reading: "unaffected" holds only for a
    caller that wires SOMETHING. A spawn passing this flag and NO
    ``stdin=``/``stdout=``/``stderr=``/``capture_output=`` at all silently
    LOSES the child's output on Windows. CPython sets ``STARTF_USESTDHANDLES``
    only when at least one of those is given; without it the child binds its
    standard handles to the fresh, window-less console this flag allocates
    instead of inheriting the parent's, and everything the child prints goes
    into a console nobody can read. Measured, not inferred. Passing any ONE of
    them is enough -- CPython then fills the unspecified handles from the
    parent's own real handles -- so ``capture_output=True``, ``stdout=PIPE``,
    and ``stderr=PIPE`` alone are all safe. If the child's output is meant to
    reach the operator, use ``no_console_passthrough_kwargs()`` below instead
    of this function. Gate: ``coordinator_core/tests/
    test_no_output_swallowing_no_console_spawn.py``.

    GAP, not covered here: a call site that also passes ``shell=True`` needs
    the separate STARTUPINFO route (``STARTF_USESHOWWINDOW`` +
    ``wShowWindow=SW_HIDE``) instead of (or in addition to) this primitive.
    With ``shell=True``, Python spawns ``cmd.exe`` as an intermediary process,
    and ``CREATE_NO_WINDOW`` on the outer call does not suppress that
    intermediary's own console window -- verified against python.org's
    ``subprocess`` docs. Callers with ``shell=True`` must not rely on this
    function alone.

    SECOND GAP -- REPORTED, THEN UNREPRODUCED; do not cite it as measured.
    This docstring previously stated that applying the flag to a **git-bash /
    MSYS child** breaks the child's own stdio: a script doing ``echo "got: $1"``
    under ``C:\\Program Files\\Git\\bin\\bash.exe`` returning rc 0 unsuppressed
    and rc 1 under ``CREATE_NO_WINDOW``. Re-run verbatim on the same host later
    the same day, then widened to 22 comparisons across ``bin\\bash.exe``,
    ``usr\\bin\\bash.exe``, ``sh.exe`` and ``rm.EXE``, four stdio shapes, and
    LF/CRLF/``set -euo pipefail`` script variants, the flag changed no return
    code and truncated no output. The one rc 1 reachable that way comes from
    ``set -u`` with an unbound ``$1`` and is identical bare and suppressed --
    the shape that yields a spurious "rc 0 before, rc 1 after" when argv changes
    in the same edit that adds the flag. Evidence:
    ``state/audits/2026-08-07-create-no-window-git-bash-stdio-claim-does-not-reproduce.md``.

    One host is not a proof of absence, so treat a shell child as unproven
    rather than safe: prefer leaving one unsuppressed when its stdio is
    load-bearing. What does NOT rest on the retracted claim, and still holds: a
    process whose console behaviour is itself the thing under test must never be
    suppressed -- see ``coordinator_core/ops/verify_no_powershell_flash.py``'s
    exemption for the worked case.

    ``import subprocess`` is function-local, not module-scope: this module's
    own docstring declares "stdlib only (os, pathlib), zero external calls,
    zero subprocess, safe to import before the venv exists" -- a module-scope
    import here would falsify that contract for every existing importer.
    """
    if not _is_windows():
        return {}

    import subprocess

    # getattr with a fallback, not a bare attribute access: CREATE_NO_WINDOW
    # only exists on subprocess when the REAL host is Windows. _is_windows()
    # is this module's documented monkeypatch seam (see its own docstring),
    # so a test exercising this branch on a real POSIX host must not raise
    # AttributeError reaching for a Windows-only constant that isn't there.
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def no_console_passthrough_kwargs() -> dict:
    """Kwargs mapping for a spawn that must suppress the console popup AND
    still let the child's output reach this process's stdout/stderr.

    Use this, not ``no_console_creationflags()``, whenever the child writes
    something an operator is meant to see -- a passthrough delegation, a
    progress-reporting installer step, a nested test run. Use
    ``no_console_creationflags()`` when the caller captures the output itself
    (``capture_output=True``/``PIPE``) or genuinely discards it.

    Windows: the flag alone is not enough. With no ``stdout=``/``stderr=``
    passed, CPython omits ``STARTF_USESTDHANDLES``, so the child binds its
    standard handles to the fresh window-less console ``CREATE_NO_WINDOW``
    allocates rather than inheriting this process's -- the output is written
    into a console nobody can read and is lost. Handing the fds over
    explicitly restores ``STARTF_USESTDHANDLES`` and the output lands where
    the operator expects it.

    Real fds, not ``sys.stdout``/``sys.stderr``: the child inherits OS
    handles, and the fd is what a redirection (a shell ``>``, a pytest
    ``capfd``) actually moved. A detached process (``pythonw``, a service) or
    a captured-object stream has no fd at all -- there is nothing to pass
    through, so those degrade to plain inheritance rather than raising.

    POSIX: the returned mapping is the fds alone (``no_console_creationflags()``
    contributes ``{}`` there), which is what inheritance already does -- so
    this is behaviour-neutral off Windows, by construction rather than by a
    platform branch.
    """
    import sys

    kwargs: dict = dict(no_console_creationflags())
    for key, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        try:
            fd = stream.fileno()
        except (AttributeError, ValueError, OSError):
            continue
        if fd >= 0:
            kwargs[key] = fd
    return kwargs
