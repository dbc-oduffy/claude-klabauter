"""coordinator_core.warm.door.build -- compiles the native fast-path client.

Naked Python (3.11+), never bash, per CLAUDE.md's runtime-conventions rule
-- this is automation, not a hand-run command.

THE ENGINE ROOT IS A RUNTIME SIDECAR, NEVER BAKED INTO THE BINARY OR RECORDED
IN PROVENANCE. `resolve_engine_root()` (door.c) reads `door.engine-root.txt`
beside the exe (or the env override); when neither resolves the door refuses
loudly rather than guess an engine. `_compile()` produces the binary (no
interpreter and no root is baked: door.c reads `door.python.txt` beside the
exe, else `py -3`); `write_sidecar()` resolves ONE thing Python is good at and
C is not (canonical path resolution, byte-matching `election.pipe_name`'s
clone-hash input) and writes it to a small text file next to the exe. `build()`
calls both, so a local build stays one command, and a `door.exe` built once can
be copied to another box and repointed with a fresh `write_sidecar()` call and
NO recompile. `engine_root` stays a build argument only for the published-engine
check, the sidecar and the engine stamp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "door.c"

#: The OS-agnostic half, shared verbatim with the POSIX door and compiled
#: alongside `door.c`. Unlike `_SOURCE` it is NOT rendered through
#: `_render()` -- it carries no build-time placeholders, and must not, since
#: the same file is compiled for a platform `build.py` never runs on.
_CORE_SOURCE = Path(__file__).resolve().parent / "door_core.c"
_CORE_HEADER = Path(__file__).resolve().parent / "door_core.h"

#: Generated X-macro table of forwarded env-var names, `#include`d by both
#: `door.c` and `door_posix.c` -- a real compile input (change the table,
#: get a different binary), so it belongs in `sources` exactly like
#: `_CORE_HEADER`, not just a data file that happens to sit next to them.
_ENV_SET_HEADER = Path(__file__).resolve().parent / "door_env_set.h"

#: Every file compiled into `door.exe`, in the order `write_provenance`
#: records them. `door_install.committed_prebuilt_source_drift` reads the
#: same tuple, so a new translation unit reaches both with one edit.
SOURCES = (_SOURCE, _CORE_SOURCE, _CORE_HEADER, _ENV_SET_HEADER)

#: Must equal door.c's `ENGINE_ROOT_SIDECAR_FILENAME` verbatim -- the two
#: are never derived from a shared constant because one is a C wide-string
#: macro and the other a Python `Path` component; keep them in lockstep by
#: hand, and grep both on change.
SIDECAR_FILENAME = "door.engine-root.txt"

#: Must equal door.c's `OWNER_SID_SIDECAR_FILENAME` and
#: `election.OWNER_SID_RECORD_NAME`. Windows only; holds a machine SID, so it is gitignored.
OWNER_SID_SIDECAR_FILENAME = "door.owner-sid.txt"

#: Provenance sidecar written next to every compiled `door.exe` -- see
#: `write_provenance()`. Filename convention: `<exe-name>.provenance.json`,
#: so it survives a `--output` pointing somewhere other than `door.exe`.
_PROVENANCE_SUFFIX = ".provenance.json"

#: Must equal door.c's `PYTHON_SIDECAR_FILENAME`. One line: the interpreter
#: the cold leg runs; absent or stale, the door falls back to `py -3`.
PYTHON_SIDECAR_FILENAME = "door.python.txt"


def _c_string_body(value: str) -> str:
    """Escapes `value` for use inside a C string literal -- backslashes
    and double quotes, the only two characters Windows paths plus a
    plain interpreter path can plausibly carry that C string syntax
    itself requires escaping."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def portable_python_bin(python_bin: Path) -> str:
    """Provenance form of an interpreter path: `~/<rest>` (posix separators)
    when it lies under the home directory, so a published sidecar never
    carries the operator's username; the resolved absolute path otherwise."""
    resolved = Path(python_bin).resolve()
    try:
        return "~/" + resolved.relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(resolved)


class HomeDirectoryUnresolvedError(ValueError):
    """A `~/` interpreter record cannot expand: the home variable is absent."""


def expand_python_bin(recorded: str) -> Path:
    """Inverse of `portable_python_bin`: expands a leading `~/` against the
    current home directory, raising `HomeDirectoryUnresolvedError` naming the
    home variable when none resolves (never a literal `~` path). Any other
    value is returned as-is, so a relative path stays relative for the caller
    to refuse."""
    if recorded.startswith(("~/", "~\\")):
        variable = "USERPROFILE" if os.name == "nt" else "HOME"
        try:
            home = Path.home()
        except (RuntimeError, KeyError):
            home = Path("~")
        if not home.is_absolute():
            raise HomeDirectoryUnresolvedError(
                f"cannot expand {recorded!r}: home directory unresolved ({variable} unset)")
        return home / recorded[2:]
    return Path(recorded)


def _render(source: str, engine_root: Path | None = None) -> str:
    """The door source as compiled: no build-time token remains, so it is
    returned verbatim. `engine_root` is accepted for caller compatibility and
    never rendered in -- door.c resolves the root and interpreter at run time
    from their sidecars."""
    return source


def _sidecar_root_string(engine_root: Path) -> str:
    """The string `write_sidecar` records for `engine_root`.

    A LITERAL symlink is recorded UNRESOLVED (`os.path.abspath`, which
    normalises `.`/`..`/cwd-relativity but never follows a symlink) --
    never `Path(engine_root).resolve()`, which would freeze whichever
    target the link happened to point at on THIS build/install and go
    stale the moment the link is repointed (incident: a cloud session's
    `/root/engine-current`, repointed post-install by
    `coordinator_core.hooks.repin_cloud_engine_root`, left every
    already-installed door dialling the ORPHANED pre-repoint target
    forever -- the sidecar is read on every invocation, but a resolved
    value baked into it defeats that by design). The door's own C-side
    `resolve_engine_root()` calls `realpath()` on whatever this writes,
    fresh, every invocation -- the OS resolves the symlink at that
    call, exactly as it would at `open()`/`exec()` time, so recording
    the link literally is what lets a later repoint be followed with NO
    sidecar rewrite. A non-symlink `engine_root` is unaffected: this
    still resolves it exactly as before (`.resolve()`), so an ordinary
    (non-cloud) install's byte-for-byte sidecar contents do not change."""
    root = Path(engine_root)
    if root.is_symlink():
        return os.path.abspath(str(root))
    return str(root.resolve())


def write_sidecar(output_exe: Path, engine_root: Path) -> Path:
    """Writes the engine-root sidecar next to `output_exe`, in the exact
    format `door.c :: read_sidecar_utf8` requires: one line, UTF-8, no
    BOM -- see `_sidecar_root_string` for what that line is exactly
    (RESOLVED for an ordinary root, LITERAL for a symlink root, so a
    later repoint of the link is followed at read time rather than
    frozen at write time). Canonicalisation of a non-symlink root still
    happens here -- door.c performs none of its own for that case -- so
    that string is what the door's clone-hash input is, byte for byte,
    until the sidecar is next rewritten OR (symlink case) until the
    door's own runtime `realpath()` call resolves it fresh.

    `newline=""` on the write is deliberate: Python's default text-mode
    write would translate a bare `\\n` to `\\r\\n` on Windows, which is
    harmless here (the C reader trims trailing `\\r`/`\\n`/whitespace) but
    unnecessary -- this writes exactly the bytes the contract promises.

    A LIVE DOOR READS THIS FILE ON EVERY INVOCATION, and the installer calls
    this once per named forwarder (~445 times a run). On Windows a truncating
    write while a door holds the file open is `[Errno 13]`, which failed four
    names on a real install. So: an unchanged sidecar is not rewritten, and a
    changed one lands whole via `os.replace`, retried briefly because a
    door's read holds the handle for microseconds, not across the retry.
    """
    path = _write_sidecar_atomic(
        output_exe.parent / SIDECAR_FILENAME,
        (_sidecar_root_string(engine_root) + "\n").encode("utf-8"),
    )
    if os.name == "nt":
        write_owner_sid_sidecar(output_exe)
    return path


def write_owner_sid_sidecar(output_exe: Path) -> Path:
    """Writes the owner-SID record (`OWNER_SID_SIDECAR_FILENAME`) beside
    `output_exe`: one line, the SID of the user running this build, which the
    door and `election.engine_owner_sid` read to name the warm pipe. The first
    write links a finished temp into place (a reader never sees a partial
    file); a changed record goes through `_write_sidecar_atomic`."""
    from coordinator_core.warm import election

    record = output_exe.parent / OWNER_SID_SIDECAR_FILENAME
    content = (election.current_user_sid() + "\n").encode("utf-8")
    if not record.exists():
        tmp = record.with_name(f"{record.name}.{os.getpid()}.tmp")
        tmp.write_bytes(content)
        try:
            os.link(tmp, record)
            return record
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    return _write_sidecar_atomic(record, content)


def write_python_sidecar(output_exe: Path, python_bin: Path | None = None) -> Path:
    """Writes the interpreter sidecar (`PYTHON_SIDECAR_FILENAME`) beside
    `output_exe`: one line, the absolute path of `python_bin` (default: this
    interpreter, detected, never a literal), same atomic discipline as
    `write_sidecar`. door.c ignores it when the file it names is gone."""
    interpreter = os.path.abspath(str(python_bin if python_bin is not None else sys.executable))
    return _write_sidecar_atomic(
        output_exe.parent / PYTHON_SIDECAR_FILENAME, (interpreter + "\n").encode("utf-8")
    )


def _write_sidecar_atomic(sidecar_path: Path, content: bytes) -> Path:
    try:
        if sidecar_path.read_bytes() == content:
            return sidecar_path
    except OSError:
        pass
    tmp = sidecar_path.with_name(f"{sidecar_path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    for attempt in range(_SIDECAR_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, sidecar_path)
            return sidecar_path
        except OSError as exc:
            if not isinstance(exc, PermissionError) or attempt == _SIDECAR_REPLACE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.01)
    return sidecar_path


#: `write_sidecar`'s retry bound: 10 x 10ms rides out a door's read, and a
#: file held longer than that is not a reader, so it raises.
_SIDECAR_REPLACE_ATTEMPTS = 10


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_sha256(path: Path) -> str:
    """sha256 of a door source with every CR stripped: provenance identity is
    line-ending-blind, so the writer and every verifier share this one hash."""
    return hashlib.sha256(path.read_bytes().replace(b"\r", b"")).hexdigest()


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


def build_attribution(module: str = "coordinator_core.warm.door.build") -> dict[str, object]:
    """Who built this binary and with what command, recorded in provenance so
    attribution never rests on a commit message. `builder` is the calling
    coordinator session id when one is set, else None: an unattributed build stays visibly
    unattributed rather than guessed. `command` is the build's own canonical
    invocation with `engine_root` as a placeholder (the field of that name
    carries it), never `sys.argv`: an in-process caller (publish's mirror
    rebuild) would otherwise record ITS argv -- operator paths and the source
    checkout -- into a published file."""
    from coordinator_core.session.core import attributable_session_id

    return {
        "builder": attributable_session_id() or None,
        "command": [Path(sys.executable).name, "-m", module, "<engine_root>"],
    }


def _resolve_engine_stamp(engine_root: Path, override: str | None) -> str | None:
    """`override` when given, else the stripped stamp file under `engine_root`;
    None when that file is absent (a direct `write_provenance` caller)."""
    if override is not None:
        return override
    try:
        return (Path(engine_root) / "coordinator_core" / "_engine_stamp").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return None


def _portable_flag(flag: str) -> str:
    """Records the door source directory as `<door_dir>` so a published sidecar carries no build-host path."""
    door_dir = str(_SOURCE.parent)
    return flag.replace(door_dir, "<door_dir>") if door_dir in flag else flag


def write_provenance(
    output_exe: Path, kind: str, compiler_path: str, engine_root: Path,
    *, image_sha256: str | None = None, engine_stamp: str | None = None,
    require_warm: bool = False, rendered_source_sha256: str | None = None,
    compile_flags: list[str] | None = None, python_bin: Path | None = None,
) -> Path:
    """Records, next to `output_exe`, the SHA-256 of the `door.c` this
    binary was built from plus the compiler and its version -- so a
    mismatch between the committed source and the committed binary is
    DETECTABLE BY ANYONE who reruns the build and compares, not a matter
    of taking anyone's word for it (PM ruling, 2026-08-21). Overwritten on
    every build; this is provenance for the CURRENT binary at that path,
    never a history.

    `engine_root` is read only for the engine stamp; it is never recorded,
    so the record carries no machine path.

    TWO SOURCE FIELDS, DELIBERATELY, since `door.c` stopped being the only
    input. `door_c_sha256` is retained UNCHANGED -- still exactly
    `sha256(door.c)`, so README.md's description of it stays literally true
    and prior handoffs that compared this field against a clone's `door.c`
    keep working. But it is no longer the WHOLE answer to "what source
    produced this binary", so `sources` records every file that did:
    `door.c`, `door_core.c`, `door_core.h`, and `door_env_set.h`. A verifier
    who checks only the legacy field would now miss a change to the shared
    core -- which is precisely the safety classification, so the complete
    set is the one to read. `door_env_set.h` is generated rather than
    hand-written, but a generated file compiled into the binary is a source
    like any other: change the table, get a different binary, so it belongs
    in `sources` exactly like `door_core.h`. Keeping both is the compatible
    move; silently narrowing `door_c_sha256` to mean "all sources" would
    break the comparisons that already exist.

    `image_sha256` -- THE FIELD THIS RECORD LACKED UNTIL 2026-08-30, AND WHY
    THAT WAS A DEFECT, NOT AN OMISSION. Every field above describes this
    build's INPUTS; none describes its OUTPUT. That meant a provenance
    sidecar could never be checked against the binary sitting beside it --
    not by a tool, not by a person -- because the one fact that would let a
    reader confirm "this record is still describing THIS file" was never
    recorded. A sidecar copied alongside a stale binary (or a binary
    rewritten in place by some OTHER writer after the sidecar was last
    written) then reads as authoritative when it is not: on 2026-08-30 a
    sibling repo's EM read exactly such a sidecar, correctly derived that
    the door it described predated a shipped fix, and sent a cross-repo
    memo asking for a rebuild that had already happened -- the installed
    binary's mtime/ctime were hours newer than the sidecar's, but nothing in
    the sidecar itself said so. See
    `state/bug-backlog/2026-08-30-installed-door-provenance-sidecar-describes-a-binary-that-is-not-there.yaml`
    for the full incident. `image_sha256` closes that gap: it is the
    sha256 of `output_exe` itself, taken after the binary is fully written
    to disk, so a verifier (human or `door_install.verify_installed_
    provenance`) can hash the binary next to the sidecar and compare
    against a field the record actually carries, rather than trusting an
    mtime that `copy2` routinely preserves from a much older source.

    `image_sha256` keyword: `build()` computes the digest ONCE and passes
    it here, so the finished binary is hashed a single time per build.
    Defaults to `None`/self-computed so a direct or test caller that has
    no digest handy keeps working unchanged.

    `engine_stamp` -- the override when given, else the stripped content of
    `engine_root/coordinator_core/_engine_stamp`; recorded so a door built
    for a scratch publish carries the publish commit's provenance."""
    provenance = {
        "engine_stamp": _resolve_engine_stamp(engine_root, engine_stamp),
        "require_warm": require_warm,
        "python_bin": portable_python_bin(Path(python_bin or sys.executable)),
        "rendered_source_sha256": rendered_source_sha256,
        "compile_flags": [_portable_flag(flag) for flag in (compile_flags or [])],
        "door_c_sha256": source_sha256(_SOURCE),
        "sources": {path.name: source_sha256(path) for path in SOURCES},
        "image_sha256": image_sha256 if image_sha256 is not None else _sha256_file(output_exe),
        "compiler": kind,
        "compiler_version": _compiler_version(kind, compiler_path),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **build_attribution(),
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
    # Lazy: the mirror's build.py is file-loaded by door_install, so no top-level engine import.
    from coordinator_core.temp_layout import managed_temp_dir

    with managed_temp_dir("door-verify-") as tmp:
        rebuilt = tmp / "door-rebuilt.exe"
        build(engine_root, python_bin=python_bin, compiler=compiler, output=rebuilt, windowless=False)
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


#: Name of the GUI-subsystem twin of `door.exe`, written beside it by `build()`.
WINDOWLESS_EXE_NAME = "door-w.exe"

_WINDOWLESS_CLANG_LINK = ["-Xlinker", "/SUBSYSTEM:WINDOWS", "-Xlinker", "/ENTRY:mainCRTStartup"]
_WINDOWLESS_CL_LINK = ["/SUBSYSTEM:WINDOWS", "/ENTRY:mainCRTStartup"]
# The windowless variant alone defines DOOR_WINDOWLESS (door.c pumps its
# message queue at main) and links user32 for PeekMessageW.
_WINDOWLESS_CLANG_DEFS = ["-DDOOR_WINDOWLESS"]
_WINDOWLESS_CL_DEFS = ["/DDOOR_WINDOWLESS"]
_WINDOWLESS_CLANG_LIBS = ["-luser32"]
_WINDOWLESS_CL_LIBS = ["User32.lib"]


def _compile_flags(kind: str, windowless: bool = False) -> list[str]:
    """The flag list `_compile` links with, minus the output/source operands --
    also what provenance records, so the two cannot drift."""
    if kind == "clang":
        flags = [
            "-O2", "-Wall", "-Wextra",
            *(_WINDOWLESS_CLANG_DEFS if windowless else []),
            "-I", str(_SOURCE.parent), "-ladvapi32", "-lshell32",
            *(_WINDOWLESS_CLANG_LIBS if windowless else []),
            "-Xlinker", "/Brepro",
        ]
        return flags + (_WINDOWLESS_CLANG_LINK if windowless else [])
    flags = [
        "/nologo", "/O2", "/W3",
        *(_WINDOWLESS_CL_DEFS if windowless else []),
        "/I" + str(_SOURCE.parent),
        "/link", "Advapi32.lib", "Shell32.lib",
        *(_WINDOWLESS_CL_LIBS if windowless else []),
        "/Brepro",
    ]
    return flags + (_WINDOWLESS_CL_LINK if windowless else [])


def _compile(
    kind: str, compiler: str, source_path: Path, output_path: Path, *, windowless: bool = False
) -> None:
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
            *(_WINDOWLESS_CLANG_DEFS if windowless else []),
            "-I", include_dir,
            "-o", str(output_path), str(source_path), str(_CORE_SOURCE),
            "-ladvapi32", "-lshell32",
            *(_WINDOWLESS_CLANG_LIBS if windowless else []),
            "-Xlinker", "/Brepro",
            *(_WINDOWLESS_CLANG_LINK if windowless else []),
        ]
    else:
        cmd = [
            compiler, "/nologo", "/O2", "/W3",
            *(_WINDOWLESS_CL_DEFS if windowless else []),
            f"/I{include_dir}",
            f"/Fe:{output_path}", str(source_path), str(_CORE_SOURCE),
            "/link", "Advapi32.lib", "Shell32.lib",
            *(_WINDOWLESS_CL_LIBS if windowless else []),
            "/Brepro",
            *(_WINDOWLESS_CL_LINK if windowless else []),
        ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"door build: compile timed out after 120s\ncommand: {' '.join(cmd)}")
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
    engine_stamp: str | None = None,
    require_warm: bool = False,
    windowless: bool = True,
) -> Path:
    """Compiles `door.exe` and writes its sidecar for `engine_root`, so a
    local build stays one command. `windowless` also links the same
    sources a second time as the GUI-subsystem `door-w.exe` (plus its
    provenance) beside `output`; hook doors run under it so a hook fire
    allocates no conhost.exe. Returns the `door.exe` path.
    To ship the SAME compiled `door.exe`
    to another box, copy it there and call `write_sidecar()` again for
    that box's own engine root -- no recompile, no compiler needed on
    that box.

    `engine_root` is NOT baked into the binary or recorded in provenance: the
    runtime sidecar is the only engine-root source, and a door with none
    refuses loudly."""
    if require_warm and sys.platform != "win32":
        raise SystemExit("door build: REQUIRED warm mode is supported only on Windows")
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
    rendered = _render(source_text, engine_root)
    if require_warm:
        rendered = "#define DOOR_PRIVATE_REQUIRED 1\n" + rendered

    # Lazy: the mirror's build.py is file-loaded by door_install, so no top-level engine import.
    from coordinator_core.temp_layout import managed_temp_dir

    variants = [(output, False)]
    if windowless and not require_warm:
        variants.append((output.parent / WINDOWLESS_EXE_NAME, True))

    with managed_temp_dir("door-generated-") as tmp:
        generated = tmp / "door_generated.c"
        generated.write_text(rendered, encoding="utf-8", newline="\n")
        for target, gui in variants:
            _compile(kind, compiler_path, generated, target, windowless=gui)

    write_sidecar(output, engine_root)
    write_python_sidecar(output, python_bin)
    for target, gui in variants:
        write_provenance(
            target, kind, compiler_path, engine_root,
            image_sha256=_sha256_file(target), engine_stamp=engine_stamp,
            require_warm=require_warm, python_bin=python_bin,
            rendered_source_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            compile_flags=_compile_flags(kind, gui),
        )

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
        help="Python interpreter recorded in the door.python.txt sidecar (default: this interpreter).",
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
    parser.add_argument("--require-warm", action="store_true", help="Bake private REQUIRED warm-only mode (Windows only; no bootstrap or cold replay).")
    args = parser.parse_args(argv)
    if args.verify and args.require_warm:
        parser.error("--verify does not verify private REQUIRED artifacts")

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
        require_warm=args.require_warm,
    )
    print(f"door build: wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
