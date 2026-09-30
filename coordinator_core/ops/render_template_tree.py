"""Renders a template tree (e.g. a content-root skeleton) into a launchable
target directory, resolving the DoE root through the tiered precedence
documented on `_resolve_content_root` rather than a single hardcoded env var.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import resolve_machine_local_cli
from coordinator_core.data_root import content_root_for
from coordinator_core import launchable
from coordinator_core.launchable import resolve_launchable
from coordinator_core.machine_resolver import registry_get as _registry_get
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import is_executable, no_console_creationflags, no_console_passthrough_kwargs

_PROG = "render-template-tree.sh"


def _resolve_content_root() -> "tuple[Optional[str], int]":
    """Resolve the DoE clone root. Returns (root_or_None, exit_code_on_failure).

    Tier 1: CONTENT_ROOT env var (permanent legacy alias — wins first when both
        CONTENT_ROOT and REPO_CONTENT_ROOT are set, per coordinator_registry.content_root()).
    Tier 2: REPO_CONTENT_ROOT env var (operator override).
    Tier 3: `machine-local get repos.content_root` (registry).
    Mirrors coordinator_core.ops.gen_content_root_pointer's resolution order and
    coordinator_registry.content_root()'s precedence.

    CONTENT_ROOT was previously
    missing from this hand-rolled resolver, silently dropping the legacy-alias
    rung the shared coordinator_registry.content_root() honors.

    Tier 3 itself tries `registry_get` first, zero-spawn, and falls back to
    the `machine-local get` CLI only on a miss -- `registry_get` alone
    doesn't reach the CLI's autodiscovery/`path-exceptions.toml` rungs, and
    this repo's own `.coordinator-dev-repo` marker proves autodiscovery is
    live for `repos.content_root` on a real machine, not a hypothetical
    (2026-08-16 review finding).
    """
    content_root_override = os.environ.get("CONTENT_ROOT", "")
    if content_root_override:
        return content_root_override, 0

    env_override = os.environ.get("REPO_CONTENT_ROOT", "")
    if env_override:
        return env_override, 0

    value = _registry_get("repos.content_root") or ""
    if not value:
        ml_bin = resolve_machine_local_cli()
        if ml_bin is not None:
            try:
                proc = subprocess.run(
                    [ml_bin, "get", "repos.content_root"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    **no_console_creationflags(),
                )
                if proc.returncode == 0:
                    value = proc.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
    if not value:
        print(
            f"{_PROG}: could not resolve repos.content_root via the registry",
            file=sys.stderr,
        )
        return None, 1
    return value, 0


def _co_located_render_single() -> Optional[str]:
    """Locate render-template.py co-located in THIS repo's coordinator/bin.

    render-template.py migrated with render-template-tree.py in the
    coordinator/bin executable-surface migration (commit b644d5a9 in
    coordinator-content-repo) -- it is now claude-klabauter's OWN sibling executable, not
    DoE-resident content, so it is resolved relative to this repo
    unconditionally, ahead of any content-root lookup (env override or
    registry alike). REPO_CONTENT_ROOT / the registry still govern
    DoE-resident content this module has not itself absorbed (there is
    none left here, but the fallback below is kept as a compatibility
    safety net for a checkout where this co-located sibling is somehow
    absent).
    """
    this_repo_root = Path(__file__).resolve().parents[2]
    candidate = this_repo_root / "coordinator" / "bin" / "render-template.py"
    if candidate.is_file() and is_executable(candidate):
        return str(candidate)
    return None


def _find_render_single() -> Optional[str]:
    co_located = _co_located_render_single()
    if co_located is not None:
        return co_located

    content_root, rc = _resolve_content_root()
    if content_root is None:
        return None
    content_root = content_root_for(content_root)
    if content_root is None:
        print(
            f"render-template-tree: no coordinator content root under DoE root: {content_root}",
            file=sys.stderr,
        )
        return None
    candidate = str(content_root / "bin" / "render-template.py")
    if os.path.isfile(candidate) and is_executable(candidate):
        return candidate
    print(
        f"render-template-tree: cannot find executable render-template.py at: {candidate}",
        file=sys.stderr,
    )
    return None


def _contains_token_marker(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"{{" in fh.read()
    except OSError:
        print(f"skip: _contains_token_marker: with open(path, \"rb\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: render-template-tree.sh <src-tree-dir> <dst-tree-dir> [KEY=VALUE]...",
            file=sys.stderr,
        )
        return 1

    src = argv[0]
    dst = argv[1]
    kv_pairs = argv[2:]

    render_single = _find_render_single()
    if render_single is None:
        return 1

    if not os.path.isdir(src) or not os.access(src, os.R_OK):
        print(
            f"render-template-tree: src dir is not a readable directory: {src}",
            file=sys.stderr,
        )
        return 1

    if os.path.isdir(dst):
        try:
            non_empty = bool(os.listdir(dst))
        except OSError:
            non_empty = False
        if non_empty:
            print(
                f"render-template-tree: dst dir already exists and is non-empty: {dst}",
                file=sys.stderr,
            )
            return 1

    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True, copy_function=shutil.copy2)

    # -------------------------------------------------------------------
    # DR-276: declare every file the copytree above actually wrote under dst
    # (a tree-copy writing many files declares inside the loop, per the
    # sanctioned-mutating-CLIs seam). Declared AFTER the copy lands, never
    # before -- the contract is a report of what was ACTUALLY written.
    # -------------------------------------------------------------------
    for dirpath, _dirnames, filenames in os.walk(dst):
        for name in filenames:
            declare_write(os.path.join(dirpath, name))

    token_bearing: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(dst):
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            if _contains_token_marker(fpath):
                token_bearing.append(fpath)
    token_bearing.sort()

    if launchable._is_windows():
        render_single_argv = resolve_launchable(render_single)
    else:
        render_single_argv = [sys.executable, render_single]

    if token_bearing:
        proc = subprocess.run(
            [*render_single_argv, "--in-place", *token_bearing, *kv_pairs],
            **no_console_passthrough_kwargs(),
        )
        if proc.returncode != 0:
            return proc.returncode

    return 0
