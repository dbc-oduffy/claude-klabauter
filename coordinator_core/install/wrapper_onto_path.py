"""
coordinator_core.install.wrapper_onto_path -- op "install.wrapper_onto_path": copy a
caller-supplied wrapper executable into the operator's per-user bin directory and
report whether that directory is already on PATH.

Purpose: Python port of the fence's `mkdir -p ~/.local/bin && cp -p <wrapper>
~/.local/bin/ && chmod +x ...` install step (commands/install.md:1317), plus the
PATH-membership advisory check that step performed inline. This op installs a
SINGLE named source file onto the operator's PATH-eligible bin dir; it never
mutates PATH itself (advisory `on_path` in the response, not a PATH write) and
never shells to a copy/mkdir/chmod binary.

Contract (op-classification manifest row `install-wrapper-onto-path`):
    params: {wrapper_src: str, check_only: bool}
        -> {installed_path: str, modified: bool, on_path: bool, warning: str (optional)}
    `warning` is present iff `on_path` is False -- `installed_path` was
    resolved (and, off `check_only`, written) to a directory the caller
    cannot run it from without a PATH change; a bare `on_path: false` field
    was found to go unactioned by every caller of this op, so a
    non-`on_path` reader now gets an explicit, non-ignorable signal too.

Design:
    - Target dir is resolved natively per-platform (`_default_wrapper_bin_dir`),
      never a hardcoded `~/.local/bin` string -- POSIX gets `~/.local/bin`
      (the fence's own target, and pipx/uv's own convention), Windows gets
      `%LOCALAPPDATA%/Programs/bin` (a per-user, no-admin-required, PATH-eligible
      location; `~/.local/bin` is not an idiomatic Windows PATH entry).
    - PATH-membership check splits on `os.pathsep` (`;` on Windows, `:` on POSIX)
      -- never a literal `:` (the oracle's platform-hazard note).
    - `wrapper_src` is passed through `normalize_native_path()` before use, so an
      MSYS/cygdrive-form input (`/c/Users/...`) resolves correctly on Windows.

Idempotency (DEC-7 note, AC7 -- oracle-rated low/low): a second invocation with
an unchanged source is a true no-write no-op. `_install_one` compares source and
destination content via `filecmp.cmp(..., shallow=False)` before copying (same
content-diff discipline as `coordinator_core/install/substrate.py::_install_one`)
-- unlike the fence's own `cp -p`, which recopies unconditionally on every
rerun, this closes the oracle's "no content-hash short-circuit" gap. The exec
bit is unconditionally reapplied even on the identical-content branch, because
it can drift out-of-band (checkout, `core.fileMode=false`, archive extraction)
without the content changing.

Negative-spec:
    - NEVER writes to PATH itself -- `on_path` is advisory-only, matching the
      manifest's scope-verdict ("none") and the fence's own check-only PATH
      read.
    - NEVER shells to `cp`/`chmod`/`mkdir` -- `shutil.copyfile` + `Path.chmod`
      + `Path.mkdir(parents=True, exist_ok=True)` only.
    - NEVER hardcodes `~/.local/bin` as a literal string on any platform --
      the target dir is always derived via `_default_wrapper_bin_dir()`.

Spec backlink: pln-coordinator-ops-buildout-from--903224
    § Wave 2, Mandated resolvers, DEC-2, DEC-7
Spec backlink: state/audits/2026-07-22-command-payload-inventory/{op-classification,distinct-ops-new}.tsv
    row `install-wrapper-onto-path`
"""
from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.install import resolution_journal
from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.ipc import register_op

_POSIX_CLAUSE_INDEX = 0
_WINDOWS_CLAUSE_INDEX = 1
"""Clause indices into this module's own `WRITE_SURFACE.clauses` tuple --
kept as named constants rather than restating `0`/`1` at both the
declaration site and the `record_resolution` call site below."""

_POSIX_BIN_RELATIVE = (".local", "bin")
"""POSIX per-user, PATH-eligible bin dir, relative to `$HOME` -- the fence's
own target, and pipx/uv's own convention. Extracted so
`_default_wrapper_bin_dir` and `WRITE_SURFACE` read one spelling."""

_WINDOWS_BIN_RELATIVE = ("Programs", "bin")
"""Windows per-user, PATH-eligible bin dir, relative to `%LOCALAPPDATA%`
(falling back to `~/AppData/Local`) -- no-admin-required and idiomatic for a
Windows PATH entry, unlike `~/.local/bin`. Extracted so
`_default_wrapper_bin_dir` and `WRITE_SURFACE` read one spelling."""


def _default_wrapper_bin_dir() -> Path:
    """Resolve the operator's per-user, PATH-eligible bin dir natively per platform.

    POSIX: ``~/.local/bin`` (the fence's own target, and pipx/uv's convention).
    Windows: ``%LOCALAPPDATA%/Programs/bin`` -- per-user, no-admin-required, and
    idiomatic for a Windows PATH entry; ``~/.local/bin`` is not.
    """
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base.joinpath(*_WINDOWS_BIN_RELATIVE)
    return Path.home().joinpath(*_POSIX_BIN_RELATIVE)


def _on_path_warning(target_dir: Path) -> str:
    return (
        f"{target_dir} is not on PATH -- the installed wrapper will not run "
        "from a bare shell until this directory is added to PATH"
    )


def _on_path(target_dir: Path) -> bool:
    raw_path = os.environ.get("PATH", "")
    entries = raw_path.split(os.pathsep)
    target_resolved = str(target_dir.resolve())
    for entry in entries:
        if not entry:
            continue
        try:
            if str(Path(entry).resolve()) == target_resolved:
                return True
        except OSError:
            continue
    return False


def _install_one(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
        shutil.copyfile(src, dst)
        modified = True
    else:
        modified = False
    dst.chmod(dst.stat().st_mode | 0o111)
    return modified


@register_op("install.wrapper_onto_path")
def _install_wrapper_onto_path(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "install.wrapper_onto_path" handler (sync -- offloaded to a thread).

    Required params:
        wrapper_src (str) -- path to the wrapper executable to install.
    Optional params:
        check_only (bool, default False) -- report what would happen without
            writing anything.

    Returns {installed_path, modified, on_path, warning?} (see module
    docstring), or {"error": <str>} when wrapper_src is missing/empty or does
    not resolve to an existing file.
    """
    wrapper_src = params.get("wrapper_src")
    if not wrapper_src or not isinstance(wrapper_src, str):
        return {"error": "install.wrapper_onto_path requires a non-empty wrapper_src param"}

    check_only = bool(params.get("check_only", False))

    src = normalize_native_path(wrapper_src).expanduser().resolve()
    if not src.is_file():
        return {"error": f"wrapper_src does not resolve to an existing file: {src}"}

    target_dir = _default_wrapper_bin_dir()
    installed_path = target_dir / src.name
    on_path = _on_path(target_dir)

    if check_only:
        result = {
            "installed_path": str(installed_path),
            "modified": False,
            "on_path": on_path,
        }
        if not on_path:
            result["warning"] = _on_path_warning(target_dir)
        return result

    modified = _install_one(src, installed_path)

    clause_index = _WINDOWS_CLAUSE_INDEX if os.name == "nt" else _POSIX_CLAUSE_INDEX
    resolution_journal.record_resolution(
        "wrapper-onto-path",
        clause_index,
        [WriteSurfaceEntry(kind="file-path", path=str(installed_path))],
    )

    result = {
        "installed_path": str(installed_path),
        "modified": modified,
        "on_path": on_path,
    }
    if not on_path:
        result["warning"] = _on_path_warning(target_dir)
    return result


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="wrapper-onto-path",
    source_module="coordinator_core.install.wrapper_onto_path",
    clauses=(
        ShapedClause(
            discovered_by="Path(params['wrapper_src']).name (the installed leaf filename)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<home>/" + "/".join(_POSIX_BIN_RELATIVE) + "/<wrapper-name>",
                reason="_install_one (POSIX branch): shutil.copyfile + chmod +x, content-diff short-circuited",
            ),
        ),
        # `%LOCALAPPDATA%` (falling back to `~/AppData/Local`) instead of
        ShapedClause(
            discovered_by="Path(params['wrapper_src']).name (the installed leaf filename)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<local-appdata>/" + "/".join(_WINDOWS_BIN_RELATIVE) + "/<wrapper-name>",
                reason="_install_one (Windows branch): shutil.copyfile + chmod +x, content-diff short-circuited",
            ),
        ),
    ),
)
"""This op never mutates PATH itself -- `_on_path` is a read-only advisory
check (see module docstring's Negative-spec), and this module does not call
into `shell_rc_guard` at all, so no rc-block surface is declared here."""
