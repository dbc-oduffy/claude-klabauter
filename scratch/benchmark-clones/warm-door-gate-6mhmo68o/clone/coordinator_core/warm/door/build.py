"""coordinator_core.warm.door.build -- compiles the native fast-path client.

Naked Python (3.11+), never bash, per CLAUDE.md's runtime-conventions rule
-- this is automation, not a hand-run command.

THE ENGINE ROOT IS RUNTIME-RESOLVED FOR PIPE DERIVATION, BAKED ONLY AS A
LAST-RESORT FALLBACK (2026-08-21 revision, then corrected same day -- see
door.c's module docstring, "WHY THE ENGINE ROOT IS RESOLVED AT RUNTIME"
and `BUILD_ENGINE_ROOT_W`'s own comment). Baking it as the ONLY source
meant `door.exe` could only be produced BY compiling ON the target
machine -- unacceptable as an install-chain dependency. But the first fix
for that (making the fallback need no engine root at all, via a bare
`python -m coordinator_core.invoke`) was ITSELF a correctness regression:
a bare `-m` resolves through this box's ambient editable-install pin,
silently executing the LIVE working tree instead of a published engine
(DR-315 §2 violated through a side door) -- caught before it shipped,
verified directly rather than assumed. The engine root is therefore
carried TWICE, for two different jobs: `resolve_engine_root()` (runtime,
sidecar/env, door.c) is the ONLY source for pipe derivation, always
preferred; `BUILD_ENGINE_ROOT_W` (baked here, at build time) is consulted
by `fall_through()` ONLY when that runtime resolution fails, so a
corrupted sidecar still has a correct engine to fall back to rather than
silently picking up whatever the ambient interpreter happens to be pinned
to. `_compile()` produces the binary (also baking the fallback Python
interpreter path, same convention `coordinator-invoke.cmd` uses for
`__PYTHON_BIN__`); `write_sidecar()` resolves ONE thing Python is good at
and C is not (canonical path resolution: `Path(engine_root).resolve()`
must byte-match `election.pipe_name`'s own clone-hash input) and writes
it to a small text file next to the exe, read at door.exe's OWN runtime.
`build()` calls both, so a local build stays one command -- a `door.exe`
built once can still be copied to another box and repointed with a fresh
`write_sidecar()` call and NO recompile; only the FALLBACK target changes
if that copy is never given its own sidecar at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "door.c"

#: The OS-agnostic half, shared verbatim with the POSIX door and compiled
#: alongside `door.c`. Unlike `_SOURCE` it is NOT rendered through
#: `_render()` -- it carries no build-time placeholders, and must not, since
#: the same file is compiled for a platform `build.py` never runs on.
_CORE_SOURCE = Path(__file__).resolve().parent / "door_core.c"
_CORE_HEADER = Path(__file__).resolve().parent / "door_core.h"

#: Must equal door.c's `ENGINE_ROOT_SIDECAR_FILENAME` verbatim -- the two
#: are never derived from a shared constant because one is a C wide-string
#: macro and the other a Python `Path` component; keep them in lockstep by
#: hand, and grep both on change.
SIDECAR_FILENAME = "door.engine-root.txt"

#: Provenance sidecar written next to every compiled `door.exe` -- see
#: `write_provenance()`. Filename convention: `<exe-name>.provenance.json`,
#: so it survives a `--output` pointing somewhere other than `door.exe`.
_PROVENANCE_SUFFIX = ".provenance.json"

_PLACEHOLDERS = {
    "__PYTHON_BIN_W__": "python_bin_w",
    "__BUILD_ENGINE_ROOT_W__": "engine_root_w",
}


def _c_string_body(value: str) -> str:
    """Escapes `value` for use inside a C string literal -- backslashes
    and double quotes, the only two characters Windows paths plus a
    plain interpreter path can plausibly carry that C string syntax
    itself requires escaping."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render(source: str, python_bin: Path, engine_root: Path) -> str:
    """`engine_root` is baked as `BUILD_ENGINE_ROOT_W` -- see that macro's
    own comment in door.c: a LAST-RESORT fallback script-path source, used
    by `fall_through()` only when the runtime sidecar/env resolution
    fails, never for pipe derivation (that stays entirely runtime,
    per `resolve_engine_root()`). Baking it back in (2026-08-21
    correctness fix) is deliberate, not a regression to the pre-sidecar
    design: without SOME known-good root, a corrupted sidecar would have
    no engine to fall back to at all, and the wrong prior fix for that gap
    (`-m coordinator_core.invoke`, silently resolving the ambient live
    tree) is exactly the defect this baking avoids reintroducing."""
    resolved_python = str(python_bin.resolve())
    resolved_root = str(Path(engine_root).resolve())
    substitutions = {
        "__PYTHON_BIN_W__": _c_string_body(resolved_python),
        "__BUILD_ENGINE_ROOT_W__": _c_string_body(resolved_root),
    }
    rendered = source
    for token, value in substitutions.items():
        rendered = rendered.replace(token, value)
    return rendered


def write_sidecar(output_exe: Path, engine_root: Path) -> Path:
    """Writes the engine-root sidecar next to `output_exe`, in the exact
    format `door.c :: read_sidecar_utf8` requires: one line, the ALREADY-
    RESOLVED path (`Path(engine_root).resolve()`), UTF-8, no BOM. This is
    the sole place that canonicalisation happens -- door.c performs none
    of its own, so this string is what the door's clone-hash input is,
    byte for byte, forever until the sidecar is rewritten.

    `newline=""` on the write is deliberate: Python's default text-mode
    write would translate a bare `\\n` to `\\r\\n` on Windows, which is
    harmless here (the C reader trims trailing `\\r`/`\\n`/whitespace) but
    unnecessary -- this writes exactly the bytes the contract promises.
    """
    resolved = str(Path(engine_root).resolve())
    sidecar_path = output_exe.parent / SIDECAR_FILENAME
    sidecar_path.write_text(resolved + "\n", encoding="utf-8", newline="")
    return sidecar_path


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compiler_version(kind: str, compiler_path: str) -> str:
    """First line of `<compiler> --version` -- human-identifiable ("clang
    version 22.1.2 ..." / "Microsoft (R) C/C++ Optimizing Compiler Version
    19.NN..."), not parsed further. Best-effort: returns a placeholder
    string rather than raising if the probe itself fails, since this is
    provenance metadata, not a build precondition."""
    try:
        proc = subprocess.run(
            [compiler_path, "--version"] if kind == "clang" else [compiler_path],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (proc.stdout or proc.stderr or "").strip()
        first_line = text.splitlines()[0] if text else ""
        return first_line or "(version probe returned nothing)"
    except Exception as exc:  # noqa: BLE001 -- provenance metadata, never fatal
        return f"(version probe failed: {exc!r})"


def write_provenance(
    output_exe: Path, source_path: Path, kind: str, compiler_path: str, engine_root: Path
) -> Path:
    """Records, next to `output_exe`, the SHA-256 of the `door.c` this
    binary was built from plus the compiler and its version -- so a
    mismatch between the committed source and the committed binary is
    DETECTABLE BY ANYONE who reruns the build and compares, not a matter
    of taking anyone's word for it (PM ruling, 2026-08-21). Overwritten on
    every build; this is provenance for the CURRENT binary at that path,
    never a history.

    `engine_root` is recorded too -- since 2026-08-21 it is baked into the
    binary as `BUILD_ENGINE_ROOT_W` (a per-build-machine default, see
    door.c's own comment), so it is an INPUT `--verify` needs to
    reproduce this exact binary, not incidental metadata. Without it,
    `door.exe.provenance.json` would tell a verifier the source hash and
    compiler but not the one other argument required to actually get a
    MATCH.

    TWO SOURCE FIELDS, DELIBERATELY, since `door.c` stopped being the only
    input. `door_c_sha256` is retained UNCHANGED -- still exactly
    `sha256(door.c)`, so README.md's description of it stays literally true
    and prior handoffs that compared this field against a clone's `door.c`
    keep working. But it is no longer the WHOLE answer to "what source
    produced this binary", so `sources` records every file that did:
    `door.c`, `door_core.c`, and `door_core.h`. A verifier who checks only
    the legacy field would now miss a change to the shared core -- which is
    precisely the safety classification, so the complete set is the one to
    read. Keeping both is the compatible move; silently narrowing
    `door_c_sha256` to mean "all sources" would break the comparisons that
    already exist."""
    provenance = {
        "door_c_sha256": _sha256_file(source_path),
        "sources": {
            path.name: _sha256_file(path)
            for path in (source_path, _CORE_SOURCE, _CORE_HEADER)
        },
        "compiler": kind,
        "compiler_version": _compiler_version(kind, compiler_path),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine_root": str(Path(engine_root).resolve()),
    }
    provenance_path = output_exe.parent / (output_exe.name + _PROVENANCE_SUFFIX)
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return provenance_path


def verify_reproducible(
    engine_root: Path,
    *,
    python_bin: Path | None = None,
    compiler: str | None = None,
    prebuilt: Path | None = None,
) -> bool:
    """Rebuilds `door.exe` from the CURRENT on-disk `door.c` into a
    throwaway temp path and compares its SHA-256 against `prebuilt`
    (default: `coordinator_core/warm/door/door.exe`, the committed
    binary). Returns True iff they match.

    This is the verification the PM's ruling asks for: "rebuild it
    yourself and see it work; here is the source hash it came from" --
    not bit-for-bit determinism across different compilers/toolchains
    (explicitly out of scope, a research project this does not attempt),
    just a same-compiler rebuild from the same source producing the same
    bytes. Relies on `_compile()`'s `/Brepro` linker flag, verified
    directly (2026-08-21) to make that rebuild deterministic.

    Raises `SystemExit` (via `build()`) on a genuine build failure --
    returns False only for an honest hash MISMATCH, never masking a
    broken build as "not reproducible".
    """
    prebuilt = prebuilt if prebuilt is not None else (_SOURCE.parent / "door.exe")
    if not prebuilt.exists():
        raise SystemExit(f"door build: no committed binary at {prebuilt} to verify against")
    with tempfile.TemporaryDirectory() as tmp:
        rebuilt = Path(tmp) / "door-rebuilt.exe"
        build(engine_root, python_bin=python_bin, compiler=compiler, output=rebuilt)
        return _sha256_file(prebuilt) == _sha256_file(rebuilt)


def _find_compiler(requested: str | None) -> tuple[str, str]:
    """Returns (kind, path) where kind is "clang" or "cl". Prefers an
    explicit --compiler; otherwise clang (present on this box), then
    cl.exe (requires a Developer Prompt environment to actually link --
    not probed further here, just located)."""
    if requested:
        found = shutil.which(requested)
        if not found:
            raise SystemExit(f"door build: requested compiler {requested!r} not found on PATH")
        kind = "cl" if Path(found).stem.lower() == "cl" else "clang"
        return kind, found

    clang = shutil.which("clang")
    if clang:
        return "clang", clang
    cl = shutil.which("cl")
    if cl:
        return "cl", cl
    raise SystemExit(
        "door build: no C compiler found on PATH. Install LLVM/clang "
        "(https://releases.llvm.org/) or run from an MSVC Developer "
        "Prompt (cl.exe), then re-run this script."
    )


def _compile(kind: str, compiler: str, source_path: Path, output_path: Path) -> None:
    """`/Brepro` (both branches -- clang here targets lld-link, which
    understands the same MSVC-compatible linker flag cl.exe's own `link.exe`
    does) replaces the PE header's embedded build timestamp with a
    content-derived value, which is what makes the output BYTE-IDENTICAL
    across separate invocations of this same source -- verified directly
    (2026-08-21): two back-to-back builds from identical `door.c` differed
    in SHA-256 without this flag and matched exactly with it. This is what
    lets `door_install.install_door()` claim its shipped binary matches its
    committed source: a verifier reruns this exact command and compares
    hashes, rather than trusting an unreproducible artifact."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # `source_path` is the RENDERED door.c, written to a temp directory, so
    # its `#include "door_core.h"` cannot resolve relatively -- the include
    # path below is what makes the shared core reachable from there.
    # `door_core.c` is passed unrendered on purpose: it carries no build-time
    # placeholders, and giving it none is what lets the same object logic
    # compile identically for the POSIX door.
    include_dir = str(_SOURCE.parent)
    if kind == "clang":
        cmd = [
            compiler, "-O2", "-Wall", "-Wextra",
            "-I", include_dir,
            "-o", str(output_path), str(source_path), str(_CORE_SOURCE),
            "-ladvapi32", "-lshell32",
            "-Xlinker", "/Brepro",
        ]
    else:
        cmd = [
            compiler, "/nologo", "/O2", "/W3",
            f"/I{include_dir}",
            f"/Fe:{output_path}", str(source_path), str(_CORE_SOURCE),
            "/link", "Advapi32.lib", "Shell32.lib", "/Brepro",
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


def build(
    engine_root: Path,
    *,
    python_bin: Path | None = None,
    compiler: str | None = None,
    output: Path | None = None,
) -> Path:
    """Compiles `door.exe` and writes its sidecar for `engine_root`, so a
    local build stays one command. To ship the SAME compiled `door.exe`
    to another box, copy it there and call `write_sidecar()` again for
    that box's own engine root -- no recompile, no compiler needed on
    that box.

    `engine_root` IS also baked into the binary now, as
    `BUILD_ENGINE_ROOT_W` -- see that macro's own comment in `door.c` and
    `_render()`'s docstring here. This is narrower than the pre-2026-08-21
    design this module used to have: it is consulted ONLY as a last-resort
    fallback script-path source when the runtime sidecar/env-var
    resolution fails, never for pipe derivation, which is unconditionally
    runtime-resolved. Moving a shipped `door.exe` to point at a different
    engine (a fresh `write_sidecar()` call, no rebuild) still works exactly
    as before; the baked value only matters if that NEW sidecar later goes
    missing or unreadable, in which case falling back to the ORIGINAL
    build-time engine is still a correct, correctly-resolving choice --
    never the ambient live-tree pin a bare `-m` would silently pick up."""
    engine_root = Path(engine_root)
    if not (engine_root / "coordinator_core" / "_engine_stamp").exists():
        raise SystemExit(
            f"door build: {engine_root} carries no coordinator_core/_engine_stamp "
            "-- it is not a published engine root (see warm.engine_root.is_engine_root). "
            "The door must be built against a published engine, never a live working tree."
        )

    python_bin = Path(python_bin) if python_bin is not None else Path(sys.executable)
    output = Path(output) if output is not None else _SOURCE.parent / "door.exe"

    kind, compiler_path = _find_compiler(compiler)
    source_text = _SOURCE.read_text(encoding="utf-8")
    rendered = _render(source_text, python_bin, engine_root)

    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp) / "door_generated.c"
        generated.write_text(rendered, encoding="utf-8", newline="\n")
        _compile(kind, compiler_path, generated, output)

    write_sidecar(output, engine_root)
    write_provenance(output, _SOURCE, kind, compiler_path, engine_root)

    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the native warm-engine door (door.exe) and its engine-root "
            "sidecar. `engine_root` is both written to the sidecar (the always- "
            "preferred, runtime-resolved pipe-derivation source) and baked into "
            "the binary as a last-resort fallback script-path root, used only "
            "if a later sidecar goes missing or unreadable."
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
        help="Compiler executable to use (default: auto-detect clang, then cl).",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path for door.exe (default: coordinator_core/warm/door/door.exe).",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help=(
            "Do not write door.exe/its sidecar. Instead rebuild from the current "
            "door.c into a temp path and compare its SHA-256 against the committed "
            "coordinator_core/warm/door/door.exe (or --output, if given). Exits "
            "nonzero on MISMATCH."
        ),
    )
    args = parser.parse_args(argv)

    if args.verify:
        ok = verify_reproducible(
            args.engine_root, python_bin=args.python, compiler=args.compiler,
            prebuilt=args.output,
        )
        print(f"door build --verify: {'MATCH' if ok else 'MISMATCH'}")
        return 0 if ok else 1

    output = build(
        args.engine_root,
        python_bin=args.python,
        compiler=args.compiler,
        output=args.output,
    )
    print(f"door build: wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
