"""coordinator_core.install.door_install_posix_build -- POSIX install-time
door build step: build the native warm-engine door when a C toolchain is
present, degrade to a runnable advisory when it isn't.

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C4.md

WHY THIS EXISTS SEPARATELY FROM `door_install.py`. POSIX has no committed
prebuilt door and cannot have one -- `build_posix.py :: build()` bakes an
absolute interpreter path and engine root via `-D` flags, so a binary built
on one machine is wrong (baked paths point at the wrong box) on any other.
`door_install.py`'s prebuilt-first design exists specifically to avoid
requiring a compiler at install time; POSIX cannot honor that design with a
prebuilt, so this module gives it the only other honest option: build when a
toolchain exists, otherwise skip the door and tell the operator how to build
it themselves later. Neither branch makes a compiler an install-chain
dependency -- a miss degrades to advisory text, not a raised error.

WHY THE DETECTOR IS REUSED, NOT REIMPLEMENTED. `build_posix.py ::
_find_compiler` already carries the search order (explicit request, then
clang, then cc, then gcc) that `build_posix.build()` itself uses to pick a
compiler. Duplicating that list here as a second probe would let the two
drift -- this module detects a *build_posix.build()`-usable* toolchain by
calling the same function and catching its `SystemExit` on a miss, so
"can build" and "did build" always agree.

WHY THE ADVISORY NAMES A SCRIPT, NEVER A SLASH COMMAND. This module can run
from `scripts/setup.py`, the coldest surface in the repo -- no Claude Code
session exists yet, so a slash command names a remedy the operator cannot
invoke (CLAUDE.md § Runtime conventions,
`coordinator/tests/test_cold_path_remediation_is_runnable.py`). The advisory
below is a `python3 <path>` command line the operator can paste directly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.warm.door import build_posix

__all__ = ["PosixDoorBuildResult", "has_posix_compiler", "build_or_advise"]


class PosixDoorBuildResult(NamedTuple):
    """Outcome of `build_or_advise()`. Exactly one of `output`/`advisory` is
    set: a build hit populates `output` and leaves `advisory` `None`; a
    toolchain miss populates `advisory` and leaves `output` `None`."""

    built: bool
    output: Optional[Path]
    advisory: Optional[str]


def has_posix_compiler(compiler: Optional[str] = None) -> bool:
    """Non-raising presence check for a `build_posix.build()`-usable C
    toolchain. Delegates to `build_posix.py :: _find_compiler` -- the same
    detector `build()` itself calls -- rather than re-listing its candidate
    compilers (`clang`, `cc`, `gcc`) here, so this probe can never drift out
    of sync with what a build actually uses. `_find_compiler` raises
    `SystemExit` on a miss; that is the only signal converted to `False`
    here, not caught more broadly."""
    try:
        build_posix._find_compiler(compiler)
    except SystemExit:
        return False
    return True


def build_or_advise(
    engine_root: Path,
    *,
    python_bin: Optional[Path] = None,
    compiler: Optional[str] = None,
    output: Optional[Path] = None,
) -> PosixDoorBuildResult:
    """Builds the POSIX door when a toolchain is present; otherwise returns
    a non-fatal advisory naming a runnable script and returns cleanly
    (never raises on a toolchain miss -- a missing compiler is an optional
    capability gap on POSIX, not an install failure).

    Windows is not this module's concern -- `door_install.py` keeps
    prebuilt-first there unchanged, per this chunk's brief. Called on
    win32, this refuses rather than silently doing nothing, since a caller
    reaching it on Windows is itself the bug.
    """
    if sys.platform == "win32":
        raise SystemExit(
            "door_install_posix_build: POSIX-only -- Windows keeps "
            "prebuilt-first via door_install.py / warm/door/build.py."
        )

    if not has_posix_compiler(compiler):
        build_posix_script = Path(build_posix.__file__).resolve()
        engine_root_str = str(Path(engine_root).resolve())
        advisory = (
            "[door-install] no C compiler found on PATH (checked clang, cc, gcc) -- "
            "the native warm-engine door is optional on POSIX; install continues "
            "without it. Install a compiler (e.g. `xcode-select --install` on "
            f"macOS) and build it later with: python3 {build_posix_script} "
            f"{engine_root_str}"
        )
        return PosixDoorBuildResult(built=False, output=None, advisory=advisory)

    built_output = build_posix.build(
        engine_root, python_bin=python_bin, compiler=compiler, output=output
    )
    # A fresh compile is NOT guaranteed to carry the exec bit -- verified
    # directly (2026-08-22): `clang -O2 -o out t.c` under `umask 0177`
    # produces `-rw-------`, zero exec bits at all, not merely a narrowed
    # group/other mask. This is the only writer on the POSIX fresh-compile
    # install path (`scripts/setup.py` passes `bin_dst` as `output` here
    # directly -- no downstream `shutil.copy2` step to inherit a mode from,
    # unlike `door_install.py`'s prebuilt-copy branch), and this path is
    # best-effort/advisory throughout, so a stripped exec bit would fail
    # silently rather than raise. `0o755` matches the mode a default umask
    # already produces; this just stops depending on the installer's
    # ambient umask to get there.
    built_output.chmod(0o755)
    return PosixDoorBuildResult(built=True, output=built_output, advisory=None)
