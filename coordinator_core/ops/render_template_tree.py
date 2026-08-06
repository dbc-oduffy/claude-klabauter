"""
coordinator_core.ops.render_template_tree — tree-walker over render-template.py.

Purpose: copy <src-tree-dir> to <dst-tree-dir>, preserving structure and dotfiles, then
substitute {{KEY}} tokens in every file that contains them by delegating each file to
render-template.py (co-located in this repo's coordinator/bin/ as of the coordinator/bin
executable-surface migration, example-doctrine-repo commit b644d5a9 -- this module shells out to it,
exactly as the bash oracle called its sibling script). Files with no {{ tokens are left as
plain copies.

Negative-spec inheritance from render-template.py (unchanged from the bash oracle): NO
conditionals, NO loops, NO includes, NO defaults, NO escaping of braces, NO whitespace
tolerance inside {{ }}. This module is a tree-walker ONLY — it does not re-implement or
extend the token substitution logic; that logic lives entirely in render-template.py and
is invoked via subprocess exactly as the bash oracle invoked its sibling script by relative
path.

Port of: render-template-tree.sh (example-doctrine-repo 290997c7, 2026-07-22)
Spec backlink: docs/plans/2026-06-22-new-project-bootstrap-skill.md § C2

Sibling-executable resolution: render-template.py is now co-located in THIS repo
(coordinator/bin/render-template.py) and is resolved there first, relative to this repo's
own root (`Path(__file__).resolve().parents[2]`) — no subprocess, no registry lookup.
Only a checkout where the co-located sibling is somehow absent falls back to the legacy
Example-doctrine-repo-root resolution (env override, then `machine-local` registry, mirroring
coordinator_core.ops.gen_doe_root_pointer's `_resolve_doe_root` tier order) as a
compatibility safety net.

Negative-spec:
    - Does NOT reimplement render-template.py's {{KEY}} substitution logic (str.replace,
      unsubstituted-key detection, atomic write) -- delegates via subprocess, faithfully
      matching the bash oracle's own division of labor (tree-walk vs single-file-render).
    - Does NOT re-derive the DR-148 bash-version guard -- meaningless in pure Python.
    - Does NOT capture/suppress render-template.py's stdout/stderr -- inherited straight
      through, matching the bash oracle (no `2>/dev/null`, no capture).
    - On a failed per-file render, propagates that child's exact exit code and stops
      immediately (mirrors `set -euo pipefail` short-circuiting the bash oracle's while
      loop on the first non-zero render-template.sh call).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.launchable import resolve_launchable
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import is_executable

_PROG = "render-template-tree.sh"  # literal program-name prefix, matches the example-doctrine-repo filename


def _resolve_machine_local() -> Optional[str]:
    """Locate the `machine-local` CLI on PATH. Returns None if absent."""
    return shutil.which("machine-local")


def _resolve_doe_root() -> "tuple[Optional[str], int]":
    """Resolve the example-doctrine-repo clone root. Returns (root_or_None, exit_code_on_failure).

    Tier 1: DOE_ROOT env var (permanent legacy alias — wins first when both
        DOE_ROOT and REPO_EXAMPLE_DOCTRINE_REPO are set, per coordinator_registry.doe_root()).
    Tier 2: REPO_EXAMPLE_DOCTRINE_REPO env var (operator override).
    Tier 3: `machine-local get repos.example_doctrine_repo` (registry).
    Mirrors coordinator_core.ops.gen_doe_root_pointer's resolution order and
    coordinator_registry.doe_root()'s precedence.

    Review: code-reviewer (2026-07-22, Finding 4) — DOE_ROOT was previously
    missing from this hand-rolled resolver, silently dropping the legacy-alias
    rung the shared coordinator_registry.doe_root() honors.
    """
    doe_root_override = os.environ.get("DOE_ROOT", "")
    if doe_root_override:
        return doe_root_override, 0

    env_override = os.environ.get("REPO_EXAMPLE_DOCTRINE_REPO", "")
    if env_override:
        return env_override, 0

    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        print(
            f"{_PROG}: machine-local not found -- cannot locate render-template.sh",
            file=sys.stderr,
        )
        return None, 1

    try:
        proc = subprocess.run(
            [ml_bin, "get", "repos.example_doctrine_repo"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{_PROG}: machine-local invocation failed: {exc}", file=sys.stderr)
        return None, 1

    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        print(
            f"{_PROG}: could not resolve repos.example_doctrine_repo via machine-local",
            file=sys.stderr,
        )
        return None, 1
    return value, 0


def _co_located_render_single() -> Optional[str]:
    """Locate render-template.py co-located in THIS repo's coordinator/bin.

    render-template.py migrated with render-template-tree.py in the
    coordinator/bin executable-surface migration (commit b644d5a9 in
    example-doctrine-repo) -- it is now claude-klabauter's OWN sibling executable, not
    example-doctrine-repo-resident content, so it is resolved relative to this repo
    unconditionally, ahead of any example-doctrine-repo-root lookup (env override or
    registry alike). REPO_EXAMPLE_DOCTRINE_REPO / the registry still govern
    example-doctrine-repo-resident content this module has not itself absorbed (there is
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
    """Locate render-template.py: co-located first, then the example-doctrine-repo root."""
    co_located = _co_located_render_single()
    if co_located is not None:
        return co_located

    doe_root, rc = _resolve_doe_root()
    if doe_root is None:
        return None
    candidate = os.path.join(doe_root.rstrip("/"), "coordinator", "bin", "render-template.py")
    if os.path.isfile(candidate) and is_executable(candidate):
        return candidate
    print(
        f"render-template-tree: cannot find executable render-template.py at: {candidate}",
        file=sys.stderr,
    )
    return None


def _contains_token_marker(path: str) -> bool:
    """Fixed-string (not regex) check for a literal '{{' anywhere in the file.

    Mirrors the bash oracle's `grep -qF '{{' "$f"` -- binary-safe files that raise a
    decode error are treated as non-token-bearing (grep -F on binary content still scans
    byte-for-byte; reading as bytes here preserves that behavior without a UnicodeDecodeError).
    """
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

    # -------------------------------------------------------------------
    # Copy the entire tree (including dotfiles) to dst -- mirrors `cp -a src/. dst/`
    # -------------------------------------------------------------------
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

    # -------------------------------------------------------------------
    # Render every token-bearing file IN PLACE via render-template.py
    # -------------------------------------------------------------------
    token_bearing: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(dst):
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            if _contains_token_marker(fpath):
                token_bearing.append(fpath)
    token_bearing.sort()

    render_single_argv = resolve_launchable(render_single)

    for fpath in token_bearing:
        # resolve_launchable, not a bare path: render-template.py carries a shebang,
        # which Windows CreateProcess cannot exec (WinError 193).
        proc = subprocess.run([*render_single_argv, fpath, "-o", fpath, *kv_pairs])
        if proc.returncode != 0:
            return proc.returncode

    return 0
