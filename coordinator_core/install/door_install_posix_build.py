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
below is a `python3 -m <module>` command line the operator can paste
directly. **The module route, never a file path** -- `build_posix.py` does
`from .build import write_sidecar`, so `python3 <abs path>/build_posix.py`
dies on `ImportError: attempted relative import with no known parent
package` before reaching its own argparse. Naming a remedy that cannot run
is the same defect as naming a slash command, one layer down.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.warm.door import build_posix

__all__ = ["PosixDoorBuildResult", "has_posix_compiler", "build_or_advise"]


class PosixDoorBuildResult(NamedTuple):

    built: bool
    output: Optional[Path]
    advisory: Optional[str]


def has_posix_compiler(compiler: Optional[str] = None) -> bool:
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
    if sys.platform == "win32":
        raise SystemExit(
            "door_install_posix_build: POSIX-only -- Windows keeps "
            "prebuilt-first via door_install.py / warm/door/build.py."
        )

    if not has_posix_compiler(compiler):
        # `PYTHONPATH=<engine root>` is load-bearing, not decoration. The
        engine_root_str = str(Path(engine_root).resolve())
        advisory = (
            "[door-install] no C compiler found on PATH (checked clang, cc, gcc) -- "
            "the native warm-engine door is optional on POSIX; install continues "
            "without it. Install a compiler (e.g. `xcode-select --install` on "
            "macOS) and build it later with: "
            f"PYTHONPATH={engine_root_str} python3 -m "
            f"{build_posix.__name__} {engine_root_str}"
        )
        return PosixDoorBuildResult(built=False, output=None, advisory=advisory)

    built_output = build_posix.build(
        engine_root, python_bin=python_bin, compiler=compiler, output=output
    )
    built_output.chmod(0o755)
    return PosixDoorBuildResult(built=True, output=built_output, advisory=None)
