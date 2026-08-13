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
    ``.sh`` file some caller already resolved (a user-supplied
    install-health drop-in, or a sibling repo's own script resolved
    defensively across a repo boundary), exactly as it offers to launch a
    ``.js`` via ``node`` when a caller resolves one of those instead. The
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
    1. ``<script>.cmd`` twin, if present. ``coordinator-claude/coordinator/bin/`` deliberately
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

__all__ = ["resolve_launchable", "resolve_by_shebang", "which_path_ordered"]

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
    ``_INTERPRETER_BY_SUFFIX`` map: the coordinator-claude bash-clean-slate residual
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
    ``[*resolve_launchable(p), *args]`` is the complete vector. On POSIX the result is
    always ``[script_path]`` -- the shebang is authoritative there (see module
    docstring § Negative-spec).
    """
    script_path = os.fspath(script_path)

    if not _is_windows():
        return [script_path]

    cmd_twin = script_path + ".cmd"
    if os.path.isfile(cmd_twin):
        return [cmd_twin]

    shebang_prefix = _shebang_launcher(script_path)
    if shebang_prefix:
        return [*shebang_prefix, script_path]

    prefix = _interpreter_for(os.path.splitext(script_path)[1].lower())
    return [*prefix, script_path]


def _interpreter_name_from_shebang(first_line: str) -> Optional[str]:
    """Extract the bare interpreter *name* from a shebang line, or ``None``.

    Handles both the direct form (``#!/bin/bash``) and the ``env`` indirection
    form (``#!/usr/bin/env python3``) -- for the ``env`` form the interpreter
    is the first non-flag token *after* ``env``, not ``env`` itself and not
    one of ``env``'s own flags (e.g. ``-S`` in the ``env -S python3 -u``
    split-string idiom -- GNU coreutils, increasingly common as a portable
    way to pass an interpreter flag through a shebang). Returns ``None`` for
    anything that isn't a well-formed ``#!`` line (no shebang, empty
    interpreter path, an ``env`` line with only flag tokens, etc.) so callers
    can fall through to a default.

    Review: code-reviewer (Finding 2) -- the previous unconditional
    ``tokens[1]`` read mis-resolved ``env -S python3 -u`` to the interpreter
    name ``"-S"``, which does not exist on PATH and would crash the caller's
    ``subprocess.call`` (Finding 1). Trailing interpreter flags themselves
    (e.g. ``-u``) are still not propagated into the resolved argv -- this
    function resolves an interpreter *name*, not a full shebang-argument
    reproduction (see Finding 8 caveat below).
    """
    if not first_line.startswith("#!"):
        return None
    rest = first_line[2:].strip()
    if not rest:
        return None
    tokens = rest.split()
    if not tokens:
        return None
    name = os.path.basename(tokens[0])
    if name == "env":
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            name = os.path.basename(token)
            break
        else:
            return None
    return name or None


def _is_python_interpreter_name(name: str) -> bool:
    """``python``, ``python3``, ``python3.11`` -- anything shaped like a CPython
    binary name. Deliberately conservative (prefix match on ``python``) so a
    shebang naming a wrapper like ``pythonw`` still routes onto this branch.

    Caveat: the match is on a prefix, not a runnable-binary check, so a
    shebang naming a non-interpreter tool that happens to start with
    ``python`` (e.g. ``python-config``, ``pythonic-lint``) would also match
    and get redirected to ``sys.executable``, which is wrong for that name
    specifically. Low practical risk for install-health drop-ins, whose
    shebangs are hand-authored."""
    return name.lower().startswith("python")


def resolve_by_shebang(script_path: str) -> List[str]:
    """Return the COMPLETE argv that launches ``script_path`` via an EXPLICIT
    interpreter resolved from its own ``#!`` shebang line -- never a bare
    path.

    This is the seam for drop-in-script orchestrators (e.g.
    ``coordinator_core.ops.install_health_run``) that iterate a directory of
    ``*.sh``-suffixed scripts whose actual interpreter may not be bash: the
    ``.sh`` suffix is sometimes kept purely so a directory glob still finds
    the drop-in (see ``coordinator-claude/coordinator/bin/install-health/
    seed-skill-overrides.sh``, which is pure Python under a ``.sh`` name).
    Running such a script as ``bash <script>`` dies immediately on its first
    non-bash line; this function reads the shebang instead of assuming it.

    Resolution order:
        1. (Windows only) ``<script>.cmd`` twin, if present -- same tier-1
           rationale as ``resolve_launchable``.
        2. Parse the first line for a ``#!`` shebang (direct or ``env``
           form). A Python interpreter name (``python``, ``python3``,
           ``pythonX.Y``, ...) resolves to ``sys.executable`` -- NOT a PATH
           probe -- for the same reason ``_interpreter_for`` pins ``.py`` to
           ``sys.executable``: a Python parent spawning a Python child must
           stay on the interpreter it is already running under (venv/
           import-path correct by construction), because these drop-ins are
           trampolines back INTO ``coordinator_core``. Any other interpreter
           name resolves through PATH via ``shutil.which(name) or name``.
        3. No shebang, unreadable file, or an unparseable first line ->
           ``["bash"]``, preserving today's behaviour for legacy drop-ins
           that never declared a shebang.

    The returned list always ends with a path/name for the thing being
    launched, matching sibling ``resolve_launchable``'s convention: the
    script is folded into the returned vector unconditionally, so
    ``resolve_by_shebang(script_path)`` is always the complete, ready-to-run
    argv and callers need no branching. In the Windows ``.cmd``-twin tier the
    twin path itself is the complete, self-sufficient launch target and is
    returned alone (the original ``script_path`` is NOT also appended --
    passing it as a trailing argument to the twin would be wrong); in every
    other tier the resolved interpreter is followed by ``script_path``.

    Review: code-reviewer (Finding 4) -- previously this function returned an
    argv *prefix* (script excluded) in the normal case but a *complete* argv
    (script included) in the Windows-twin case, and the sole caller
    discriminated the two shapes with a fragile string-equality check
    (``launch_prefix[-1] == script + ".cmd"``). Folding the script in
    unconditionally matches ``resolve_launchable`` and removes that
    caller-side branch entirely.

    Negative-spec:
        - **Never a bare-path result for the script itself.** Unlike
          ``resolve_launchable``'s deliberate POSIX bare-path tier, this
          function must NOT rely on the OS exec loader honouring the exec
          bit + shebang, because the install-health drop-in contract
          (``coordinator_core.ops.install_health_run`` module docstring)
          explicitly promises the execute bit is NOT required for pickup --
          a drop-in script may ship mode ``0o644`` with no exec bit set.
          Passing the script as an *argument* to an
          explicit interpreter is what keeps that guarantee true regardless
          of exec bit.
        - Does not validate that the resolved interpreter exists on PATH,
          only that a name was found; ``shutil.which(name) or name`` mirrors
          ``_interpreter_for``'s own fallback idiom so a PATH populated only
          in the child process still works.
        - A binary/undecodable first line does not raise -- it is treated
          identically to "no shebang" and falls through to ``["bash"]``.
        - ``env``-form shebangs resolve only the interpreter *name*; trailing
          interpreter flags (e.g. the ``-u`` in ``env python3 -u``, or
          ``#!/bin/bash -x``) are intentionally not propagated into the
          resolved argv -- this is a best-effort interpreter-*name* resolver,
          not a full shebang-argument reproduction.

    Spec backlink: ``cross-repo/inbox/2026-07-21-claude-central-em-dr079-doe-dispositions-and-install-health-defect.md``
    """
    script_path = os.fspath(script_path)

    if _is_windows():
        cmd_twin = script_path + ".cmd"
        if os.path.isfile(cmd_twin):
            return [cmd_twin]

    try:
        with open(script_path, "rb") as fh:
            first_line_bytes = fh.readline()
        # Review: code-reviewer (Finding 3) -- strip a leading UTF-8 BOM
        # before decoding. Some Windows-side editors default to UTF-8-with-BOM
        # on save; an unstripped BOM makes the decoded line start with
        # "﻿#!...", which fails the startswith("#!") check below and
        # silently falls back to bash even for a Python-shebang script.
        if first_line_bytes.startswith(b"\xef\xbb\xbf"):
            first_line_bytes = first_line_bytes[3:]
        first_line = first_line_bytes.decode("utf-8", errors="strict").rstrip("\r\n")
    except (OSError, UnicodeDecodeError):
        return ["bash", script_path]

    name = _interpreter_name_from_shebang(first_line)
    if not name:
        return ["bash", script_path]

    if _is_python_interpreter_name(name):
        return [sys.executable, script_path]

    return [shutil.which(name) or name, script_path]
