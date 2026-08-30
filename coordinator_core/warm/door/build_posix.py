"""coordinator_core.warm.door.build_posix -- compiles the POSIX (macOS/Linux)
native fast-path client.

VERIFIED ON macOS 2026-08-22: this module built `door_posix.c` on macOS
(arm64, Apple clang 21) with no source edits, and the resulting binary served
a live warm engine. It has NOT been run on Linux. See `README-posix.md` for
the build command, what was measured, and what remains unexercised there.

WHY THIS IS A SEPARATE MODULE FROM `build.py` RATHER THAN A BRANCH IN IT.
`build.py` is Windows-shaped end to end -- wide-string placeholders
(`__PYTHON_BIN_W__`), `advapi32`/`shell32` link flags, the `/Brepro` linker
flag its reproducibility claim rests on, and an `.exe` output. A POSIX branch
inside it would have touched every one of those functions while a peer agent
was editing that file, for no reuse beyond `write_sidecar()` -- which this
module imports rather than duplicates, since the sidecar's byte format is the
one thing that genuinely must not drift between the two builders. If the two
ever need to converge, converge on the sidecar contract, not on the compile
step.

WHAT IS DELIBERATELY NOT CLAIMED HERE: reproducibility. `build.py` can assert
that a rebuild from the same `door.c` produces the same bytes, because
`/Brepro` was verified to make that true on Windows. The equivalent on
clang/ld64 is `-Wl,-no_uuid` plus `ZERO_AR_DATE`, neither of which anyone has
checked here -- so this module writes provenance (source hashes, compiler,
version) and makes no reproducibility claim at all, rather than making one
nobody has tested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .build import write_sidecar

_HERE = Path(__file__).resolve().parent
_SOURCES = (_HERE / "door_posix.c", _HERE / "door_core.c")
_HEADER = _HERE / "door_core.h"

#: Filename convention matches `build.py`'s `_PROVENANCE_SUFFIX`.
_PROVENANCE_SUFFIX = ".provenance.json"


def _shell_safe_define(value: str) -> str:
    """Escapes `value` for use inside the C string literal a `-D` flag
    carries. Backslashes and double quotes are the only two characters a
    POSIX path or an interpreter path can plausibly hold that C string
    syntax itself requires escaping -- the same two `build.py ::
    _c_string_body` handles, and for the same reason."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _find_compiler(requested: str | None) -> str:
    """Returns a compiler path. Prefers an explicit `--compiler`, then
    `clang` (what Xcode Command Line Tools install on every Mac), then
    `cc`."""
    if requested:
        found = shutil.which(requested)
        if not found:
            raise SystemExit(f"door build: requested compiler {requested!r} not found on PATH")
        return found
    for candidate in ("clang", "cc", "gcc"):
        found = shutil.which(candidate)
        if found:
            return found
    raise SystemExit(
        "door build: no C compiler found on PATH. On macOS run "
        "`xcode-select --install` to get clang, then re-run this script."
    )


def _compiler_version(compiler_path: str) -> str:
    """First line of `<compiler> --version`, human-identifiable, not parsed
    further. Best-effort: provenance metadata, never a build precondition."""
    try:
        proc = subprocess.run(
            [compiler_path, "--version"], capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (proc.stdout or proc.stderr or "").strip()
        return (text.splitlines()[0] if text else "") or "(version probe returned nothing)"
    except Exception as exc:  # noqa: BLE001 -- provenance metadata, never fatal
        return f"(version probe failed: {exc!r})"


def write_provenance(output: Path, compiler_path: str, engine_root: Path) -> Path:
    """Records, next to `output`, the SHA-256 of every source file this
    binary was built from plus the compiler and its version -- so a mismatch
    between committed source and a shipped binary is DETECTABLE BY ANYONE
    who rebuilds and compares, rather than a matter of taking anyone's word
    for it (PM ruling, 2026-08-21).

    Unlike `build.py :: write_provenance`, this does NOT underwrite a
    `--verify` rebuild-and-compare: see the module docstring on why no
    reproducibility claim is made for this toolchain.

    `image_sha256` -- same field, same reasoning as `build.py ::
    write_provenance`'s own paragraph on why an input-only record cannot be
    checked against the artifact beside it; not restated here."""
    provenance = {
        "sources": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (*_SOURCES, _HEADER)
        },
        "image_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "compiler": compiler_path,
        "compiler_version": _compiler_version(compiler_path),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine_root": str(Path(engine_root).resolve()),
        "platform": sys.platform,
        # Describes THIS ARTIFACT, not the source. It stays False on a fresh
        # build by design: a successful compile is not a successful
        # invocation, and this builder does not invoke what it produces. The
        # SOURCE is no longer unverified -- see the module docstring -- so the
        # note no longer claims it has never run anywhere.
        "verified": False,
        "verified_note": (
            "compiled here, not invoked here. This builder does not exercise "
            "the binary it writes; run the door against a live warm server, "
            "or the read-deadline suite, to establish that -- see "
            "README-posix.md."
        ),
    }
    provenance_path = output.parent / (output.name + _PROVENANCE_SUFFIX)
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return provenance_path


def build(
    engine_root: Path,
    *,
    python_bin: Path | None = None,
    compiler: str | None = None,
    output: Path | None = None,
) -> Path:
    """Compiles the POSIX `door` binary and writes its engine-root sidecar.

    `engine_root` is used for TWO different jobs, exactly as on Windows.
    It is written to the sidecar -- the ONLY source for socket derivation,
    re-read at every invocation of the binary -- and it is baked as
    `BUILD_ENGINE_ROOT`, consulted by `fall_through()` ONLY when that runtime
    resolution fails, so a corrupted sidecar still has a correct engine to
    fall back to rather than silently picking up whatever the ambient
    interpreter is pinned to.

    Refuses an unstamped root up front: the door must be built against a
    published engine, never a live working tree."""
    engine_root = Path(engine_root)
    if not (engine_root / "coordinator_core" / "_engine_stamp").exists():
        raise SystemExit(
            f"door build: {engine_root} carries no coordinator_core/_engine_stamp "
            "-- it is not a published engine root (see warm.engine_root.is_engine_root). "
            "The door must be built against a published engine, never a live working tree."
        )

    python_bin = Path(python_bin) if python_bin is not None else Path(sys.executable)
    output = Path(output) if output is not None else _HERE / "door"
    compiler_path = _find_compiler(compiler)

    resolved_python = str(python_bin.resolve())
    resolved_root = str(Path(engine_root).resolve())

    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        compiler_path,
        "-O2", "-Wall", "-Wextra", "-std=c11",
        f'-DPYTHON_BIN="{_shell_safe_define(resolved_python)}"',
        f'-DBUILD_ENGINE_ROOT="{_shell_safe_define(resolved_root)}"',
        "-o", str(output),
        *[str(path) for path in _SOURCES],
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"door build: compile failed (exit {proc.returncode})\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    sys.stderr.write(proc.stdout)
    sys.stderr.write(proc.stderr)

    # Same writer, same bytes, same contract as the Windows build -- the
    # sidecar's format is what makes the C-side clone hash byte-identical to
    # `breadcrumb.svc_dir`'s, and it must not have two implementations.
    write_sidecar(output, engine_root)
    write_provenance(output, compiler_path, engine_root)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the POSIX warm-engine door and its engine-root sidecar. "
            "Verified on macOS 2026-08-22; not yet run on Linux."
        )
    )
    parser.add_argument(
        "engine_root", type=Path,
        help=(
            "Published engine root -- written to the sidecar AND baked as the "
            "fallback default (must carry coordinator_core/_engine_stamp)."
        ),
    )
    parser.add_argument(
        "--python", type=Path, default=None,
        help="Python interpreter to bake as the fallback runtime (default: this interpreter).",
    )
    parser.add_argument(
        "--compiler", default=None,
        help="Compiler executable to use (default: auto-detect clang, then cc, then gcc).",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path (default: coordinator_core/warm/door/door).",
    )
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        raise SystemExit(
            "door build_posix: this builds the POSIX door; on Windows use "
            "coordinator_core/warm/door/build.py instead."
        )

    output = build(
        args.engine_root,
        python_bin=args.python,
        compiler=args.compiler,
        output=args.output,
    )
    print(f"door build_posix: wrote {output} (not yet invoked -- see README-posix.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
