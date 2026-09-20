"""
coordinator_core.launchable -- resolve a script path into a launchable argv vector.

Launching a script by bare path -- passing a resolved script path as ``argv[0]`` with
no interpreter -- is a bash-ism written in Python syntax: it is shell-out-by-shebang,
and it only works because the POSIX exec loader reads ``#!``. Windows
``CreateProcess`` does not. Handing it a ``.js``/``.sh``/extension-less shebang script
by bare path raises::

    OSError: [WinError 193] %1 is not a valid Win32 application

100% of the time, under cmd.exe *and* under Git Bash alike -- the constraint is the OS
exec loader, not the calling shell. A mechanical bash->Python port converts the syntax
and preserves the POSIX assumption, so this defect class survives exactly the migration
meant to remove it.

The knowledge was previously local to ``coordinator_core.plugin_health.sentinel``'s
``_resolve_cli`` (and its ``_sh_argv`` sibling), which got it right while three other
ported launch sites did not. This module promotes it to one shared seam so the
constraint is encoded once instead of rediscovered per module.

Launchable-extension-map precedent (bash/sh branch, keep-not-port):
    This module's ``.sh``/``.bash`` -> ``bash`` entries in
    ``_INTERPRETER_BY_SUFFIX``, and the ``bash``/``sh`` branch of
    ``_shebang_launcher``, are the SAME shape CLAUDE.md's own § Runtime
    conventions already blesses for ``.js`` -> ``node``: "a capability, not
    a dependency." Nothing on claude-klabauter's own build/install/test critical path
    REQUIRES bash to complete -- this module offers to correctly LAUNCH a
    ``.sh`` file some caller already resolved (a sibling repo's own script
    resolved defensively across a repo boundary), exactly as it offers to
    launch a ``.js`` via ``node`` when a caller resolves one of those instead. The
    bash entries are therefore a keep, not migration debt: contingent
    irreducibility (CLAUDE.md § Runtime conventions) does not apply here
    because nothing about claude-klabauter's OWN work depends on the target actually
    being bash -- the module's job ends at "launch what the caller found,"
    the same way the ``.js`` entry's job ends at "launch what the caller
    found" regardless of whether node happens to be installed. See
    ``docs/2026-07-29-debash-residual-sites-spec.md`` Group E for the
    disposition ruling this codifies (PM ruling 2026-07-28/29: keep, document,
    do NOT add a CLAUDE.md carve-out entry -- a capability map is not a
    shell-out carve-out).

Call sites become::

    run([*resolve_launchable(script), *args])

Resolution order (Windows only -- see the POSIX note below):
    1. ``<script>.cmd`` twin, if present. ``DoE-claude/coordinator/bin/`` deliberately
       ships ``.cmd`` twins alongside its shebang scripts (see
       ``coordinator_core.install.substrate`` Step C10a-2) precisely because
       CreateProcess cannot exec a shebang. Prefer the twin when it exists: it is the
       substrate's own sanctioned Windows entry point and may carry setup the raw
       script does not.
    2. Shebang sniff -- read the script's first line and resolve its DECLARED
       interpreter (python/bash/sh/node), when readable and recognised. Catches
       transitional oracles whose CONTENT was ported (e.g. bash -> Python) before
       their filename caught up -- see ``_shebang_launcher``'s docstring.
    3. Interpreter prefix keyed on file extension (``.js`` -> node, ``.sh`` -> bash,
       ``.py`` -> this interpreter, ...) -- used when the file is unreadable/absent
       or its shebang is unrecognised.
    4. Bare path -- nothing better is known; let the failure be the caller's, loud.

Negative-spec:
    - **POSIX is bare-path, deliberately.** On POSIX the shebang line is authoritative
      and strictly more informative than our extension map: ``#!/usr/bin/env node
      --experimental-x``, or a ``.sh`` file carrying a ``#!/bin/zsh`` shebang, would
      both be mis-launched by a naive extension-keyed prefix. We only override the
      loader on the platform that has no loader to speak of. The Windows-only shebang
      sniff (tier 2) is a DIFFERENT mechanism from POSIX bare-path exec -- it reads the
      file ourselves rather than relying on the OS loader, and only for the small
      python/bash/sh/node vocabulary we recognise; anything else falls through to the
      extension map exactly as before.
    - Does NOT validate that the interpreter exists, that the script exists, or that
      the script is executable. It is a pure argv-shape function -- callers keep their
      own existence checks and their own error reporting.
    - Does NOT set creationflags / console-suppression. Orthogonal concern; see
      ``coordinator_core.write_guards.nudge_windows_subprocess_popup``.

Spec backlink: ``cross-repo/archive/2026-07-20-example-market-data-repo-em-engine-argv-vector-script-launch-windows.md``
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from typing import List, Optional

__all__ = ["resolve_launchable", "which_path_ordered"]

# Extension -> interpreter *name* (resolved through PATH at call time). Keyed on the
# lowercased suffix; extension-less scripts intentionally have no entry and fall
# through to the bare-path tier.
_INTERPRETER_BY_SUFFIX = {
    ".js": "node",
    ".cjs": "node",
    ".mjs": "node",
    ".sh": "bash",
    ".bash": "bash",
}


def _is_windows() -> bool:
    """Windows-ness as a seam, not an inline ``os.name`` read.

    Tests must exercise BOTH branches on a single host -- a suite that skipped the nt
    branch off-Windows would test nothing this module exists for. Monkeypatching
    ``os.name`` itself is not an option: ``pathlib`` keys its concrete-class selection
    on it and blows up mid-test. So the platform check lives behind one patchable
    function.
    """
    return os.name == "nt"


_SHEBANG_RE = re.compile(r"^#!\s*\S*[/\\](env\s+)?(?P<name>\S+)")


def _shebang_launcher(script_path: str) -> List[str]:
    """Sniff ``script_path``'s shebang line for its ACTUAL interpreter.

    Returns an argv prefix (``[interpreter]``), or ``[]`` when the file is
    unreadable, has no shebang, or names an interpreter we don't recognise.

    Windows ``CreateProcess`` can't honor a shebang directly (see module
    docstring) -- on POSIX this helper is never consulted, since the OS
    already does the honoring, more completely than we ever could. On
    Windows it is checked BEFORE the extension-keyed
    ``_INTERPRETER_BY_SUFFIX`` map: the DoE bash-clean-slate residual
    migration (``docs/plans/2026-07-16-bash-clean-slate-residual-migration.md``)
    ported several ``bin/*.sh`` oracles' CONTENT to Python before their
    filename caught up, leaving transitional ``.sh``-named files whose body
    is a Python script. A naive extension guess forces ``bash <file>.sh`` on
    those, which then tries to interpret the Python body as shell syntax
    (``import: command not found``) -- a Windows-only failure mode invisible
    on POSIX, where the shebang is honored regardless of the ``.sh`` name.
    Reading the shebang directly resolves the file's true interpreter
    regardless of what its extension claims.
    """
    try:
        with open(script_path, "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline()
    except OSError:
        return []
    m = _SHEBANG_RE.match(first_line)
    if not m:
        return []
    name = m.group("name")
    if "python" in name:
        return [sys.executable]
    if name in ("bash", "sh"):
        return [shutil.which("bash") or "bash"]
    if name == "node":
        return [shutil.which("node") or "node"]
    return []


def _interpreter_for(suffix: str) -> List[str]:
    """argv prefix for ``suffix``, or ``[]`` when no interpreter is known.

    ``.py`` resolves to ``sys.executable`` rather than a PATH probe: a Python parent
    spawning a Python child should stay on the same interpreter it is already running
    under (venv-correct by construction), which is exactly what ``sys.executable``
    guarantees and what a bare ``python`` on PATH does not.
    """
    if suffix == ".py":
        return [sys.executable]
    name = _INTERPRETER_BY_SUFFIX.get(suffix)
    if not name:
        return []
    # shutil.which keeps the vector absolute where possible; fall back to the bare
    # name so a PATH that is populated in the child but not the parent still works.
    return [shutil.which(name) or name]


def which_path_ordered(name: str, *, extensions: Optional[List[str]] = None) -> Optional[str]:
    """PATH-order-preserving lookup for ``name``, directory-major, extension-minor.

    ``shutil.which`` gets two related cases wrong on Windows:

    - An extensionless command that has no ``PATHEXT``-suffixed twin at all in its
      OWN directory but DOES have one further along ``PATH`` reports the far match
      first, because CPython's implementation checks ``PATHEXT`` candidates across
      the WHOLE ``PATH`` before ever falling back to a bare-name pass. That gets
      search order backwards: PATH precedence means an EARLIER directory always
      wins, regardless of which candidate shape (suffixed vs. bare) matched there.
    - A name that already ends in its own non-``PATHEXT`` extension (e.g. a
      ``.sh`` test shim) still gets ``PATHEXT`` entries appended
      (``foo.sh.COM``, ``foo.sh.EXE``, ...) since ``.sh`` isn't itself a
      ``PATHEXT`` member -- so it never tries the literal filename at all.

    This walks ``PATH`` ourselves, one directory at a time: within each directory,
    try ``name`` suffixed with each of ``extensions`` (in order), THEN the bare
    ``name``, before advancing to the next directory. Never checks one candidate
    shape across all directories before another.

    ``extensions`` defaults to ``PATHEXT`` (split on ``os.pathsep``) on Windows and
    ``[]`` on POSIX, matching ``shutil.which``'s own platform default. Pass ``[]``
    explicitly to force a bare-name-only search even on Windows -- e.g. when
    ``name`` already carries a full, specific filename (such as a ``.sh`` shim) and
    no further extension should ever be appended.

    Returns the first existing regular-file candidate, or ``None`` if nothing
    matched anywhere on ``PATH``.
    """
    if extensions is None:
        extensions = os.environ.get("PATHEXT", "").split(os.pathsep) if _is_windows() else []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidates = [os.path.join(directory, name + ext) for ext in extensions]
        candidates.append(os.path.join(directory, name))
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
    return None


def resolve_launchable(script_path: str) -> List[str]:
    """Return the argv prefix that correctly launches ``script_path`` on this OS.

    The returned list always ends with a path/name for the thing being launched, so
    ``[*resolve_launchable(p), *args]`` is the complete vector.

    POSIX is no longer "always ``[script_path]``, the shebang is authoritative"
    (2026-08-13). The POSIX-exec drain
    (docs/plans/2026-08-13-grind-the-posix-exec-baseline-to-zero.md, chunk C6)
    renames ``coordinator/bin`` entrypoints to ``<name>.py`` and strips BOTH the
    shebang and the exec bit, so a caller holding the old bare path gets "can't
    open file", and a caller holding the new path gets an exec-format error from
    a file with no shebang to be authoritative. Two probes close both, in the
    order that keeps a still-extensionless name on its existing path:

      1. bare path missing -> try ``<path>.py``
      2. resolved target not executable -> prefix ``sys.executable``

    This is the same absorption ``exec_cli`` took for the installed forwarders in
    ``23d162e6c4ff``; this function is the repo-local half of that surface, and
    peers hit it within the hour on three separate CLIs in one ceremony.
    """
    script_path = os.fspath(script_path)

    if not os.path.isfile(script_path) and not script_path.endswith(".py"):
        suffixed = script_path + ".py"
        if os.path.isfile(suffixed):
            script_path = suffixed

    if not _is_windows():
        # Narrowly `.py`: the drain strips shebang+exec bit from Python
        # entrypoints only, so this is the one extension whose non-executable
        # form is known to want `sys.executable`. Prefixing any other
        # non-executable file would hand `python3` a `.js`/`.sh` it cannot run
        # — a worse failure than the exec-format error it replaces.
        # The leading `os.name != "nt"` is redundant against the enclosing
        # `not _is_windows()` and is deliberate: `check_posix_exec_assumptions`'s
        # `posix_mode_bits` class recognizes a literal `os.name`/`sys.platform`
        # test or short-circuit and-chain as a Windows guard, but NOT a
        # project-local wrapper like `_is_windows()` (that limit is named in its
        # own docstring). Spelling the guard the detector understands keeps this
        # correct cross-platform code out of the gate without an exemption.
        if (
            os.name != "nt"
            and script_path.endswith(".py")
            and os.path.isfile(script_path)
            and not os.access(script_path, os.X_OK)
        ):
            return [sys.executable, script_path]
        return [script_path]

    cmd_twin = script_path + ".cmd"
    if os.path.isfile(cmd_twin):
        return [cmd_twin]

    shebang_prefix = _shebang_launcher(script_path)
    if shebang_prefix:
        return [*shebang_prefix, script_path]

    prefix = _interpreter_for(os.path.splitext(script_path)[1].lower())
    return [*prefix, script_path]
