"""Shared resolution of an installed settings-home CLI forwarder, plus the argv
form that actually launches whichever variant resolved.

Ported from DoE-claude `coordinator/hooks/scripts/_forwarder_resolve.py` per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C3.

Why this module exists: several SessionStart hooks each carried their own
copy of a probe that looked ONLY for the extensionless forwarder
(`<settings-home>/bin/<name>`), each justified by the same negative-spec --
"the `.cmd` variant is just a shim that execs the extensionless forwarder,
so resolving it would add an indirection for nothing." That reasoning was
sound for the generation of forwarders it was written against, where the
extensionless naked-Python script was the real target on every platform.

It is no longer true. The native-forwarder generation (see
`<settings-home>/bin/_native-forwarder-manifest.json`) emits a compiled
`.exe` per name on Windows and does NOT leave an extensionless script
beside it. Every probe shaped the old way therefore returns None on
Windows, and because consumers are fail-open by design, they degrade in
total silence -- no crash, no banner, no exit-code change.

Negative-spec: does NOT resolve `.cmd`/`.bat`. Windows' `CreateProcess` --
what `subprocess` uses under `shell=False` -- cannot launch either, so
resolving one would hand callers a path they must then shell out through
`cmd.exe` to use. The extensionless script and the native `.exe` are both
directly launchable, and between them they cover every platform the
forwarder installer targets.

Negative-spec: does NOT consult `PATH` via `shutil.which`. Callers resolve
against an explicitly-passed `bin` directory so that a same-named binary
earlier on `PATH` cannot silently substitute itself for the installed
forwarder.

ADAPTATION FROM THE PORTED SOURCE: DoE's copy imported `_is_native_image`
from a sibling `_bin_impl_drift.py` module -- a LATER wave's `writes:`
(W4-C4), not present in this chunk's footprint, and itself a re-export
whose real definition already lives in this engine at
`coordinator_core.install.door_install.is_native_image` (that module's own
comment: "already pinned against the engine's
`coordinator_core.install.door_install.NATIVE_IMAGE_MAGIC`"). This module
imports that real definition directly rather than waiting on a sibling
chunk to land a re-export of it -- reuse, not a new copy; see
`coordinator_core/install/door_install.py::is_native_image` for the pinned
magic-byte set (Mach-O both endiannesses, fat/universal, ELF, PE).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from coordinator_core.install.door_install import is_native_image as _is_native_image

_FORWARDER_SUFFIXES = ("", ".exe")

# SUFFICIENT tell and never a necessary one -- see `_is_native_image`.
_NATIVE_SUFFIXES = (".exe",)


def resolve_forwarder(bin_dir: Path, name: str) -> Optional[Path]:
    for suffix in _FORWARDER_SUFFIXES:
        candidate = bin_dir / f"{name}{suffix}"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def forwarder_argv(script_path: Path, tail: "list[str] | tuple[str, ...]" = ()) -> "list[str]":
    """Build the argv that launches `script_path`, which must have come from
    `resolve_forwarder`.

    A native image is launched bare: prefixing `sys.executable` hands machine
    code to the Python interpreter, which dies on the first byte with
    `SyntaxError: Non-UTF-8 code`. A naked-Python forwarder REQUIRES the prefix --
    a bare path works on POSIX via shebang plus exec bit, but Windows'
    `CreateProcess` does not consult shebang lines and cannot launch an
    extensionless file at all.

    Negative-spec: the SUFFIX does not decide this. `.exe` proves native, but
    nothing proves the converse -- on POSIX the cut-over door occupies the BARE
    name, which is indistinguishable by name from the naked-Python forwarder it
    replaced. Suffix-only dispatch therefore fed a Mach-O image to `python3` on
    every macOS box, and because consumers of this module are fail-open it
    degraded in total silence -- the same silence this module was written to
    end, in mirror image. Ask the bytes.

    Tripwire: AN-EXTENSIONLESS-SETTINGS-HOME-BIN-ENTRY-IS-NOT-PYTHON-SOURCE.
    """
    if script_path.suffix.lower() in _NATIVE_SUFFIXES:
        return [str(script_path), *tail]
    if _is_native_image(script_path):
        return [str(script_path), *tail]
    return [sys.executable, str(script_path), *tail]
