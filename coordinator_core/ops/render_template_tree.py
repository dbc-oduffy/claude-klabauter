"""Renders a template tree (e.g. a content-root skeleton) into a launchable
target directory, resolving the content root through
`coordinator_core.content_root.read_content_root`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core._content_root_primitive import content_root_for
from coordinator_core.content_root import read_content_root
from coordinator_core import launchable
from coordinator_core.launchable import resolve_launchable
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_passthrough_kwargs

_PROG = "render-template-tree.sh"


def _co_located_render_single() -> Optional[str]:
    """Locate render-template.py co-located in THIS repo's coordinator/bin.

    render-template.py migrated with render-template-tree.py in the
    coordinator/bin executable-surface migration (commit b644d5a9 in
    coordinator-content-repo) -- it is now claude-klabauter's OWN sibling executable, not
    DoE-resident content, so it is resolved relative to this repo
    unconditionally, ahead of any content-root lookup. The content root
    still governs content this module has not itself absorbed (there is
    none left here, but the fallback below is kept as a compatibility
    safety net for a checkout where this co-located sibling is somehow
    absent).
    """
    this_repo_root = Path(__file__).resolve().parents[2]
    candidate = this_repo_root / "coordinator" / "bin" / "render-template.py"
    if candidate.is_file():
        return str(candidate)
    return None


def _find_render_single() -> Optional[str]:
    co_located = _co_located_render_single()
    if co_located is not None:
        return co_located

    root = read_content_root()
    if not root:
        print(f"{_PROG}: could not resolve the content root", file=sys.stderr)
        return None
    content_root = content_root_for(root)
    if content_root is None:
        print(
            f"render-template-tree: no coordinator content under content root: {root}",
            file=sys.stderr,
        )
        return None
    candidate = str(content_root / "bin" / "render-template.py")
    if os.path.isfile(candidate):
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
