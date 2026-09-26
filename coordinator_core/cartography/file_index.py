
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.tree import list_tracked_files

REPO_ROOT_SYSTEM = "repo-root"


def system_for_path(relpath: str) -> str:
    """Return the system name for a single repo-relative path.

    Rule: the first path-separator-delimited component of `relpath` (its
    top-level tracked directory). A path with no separator (a root-level
    file) maps to REPO_ROOT_SYSTEM. Every well-formed repo-relative path
    (as emitted by `git ls-files`) maps to exactly one system — there is no
    "unmapped" outcome for this rule.
    """
    normalized = relpath.replace("\\", "/")
    parts = normalized.split("/", 1)
    if len(parts) == 1:
        return REPO_ROOT_SYSTEM
    return parts[0]


def list_untracked_files(target_root: str | Path) -> list:
    root = path_guard(target_root, ".")
    cmd = ["git", "ls-files", "--others", "--exclude-standard"]
    from coordinator_core.win_portability import no_console_creationflags

    result = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-files --others failed under {root!r} "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    return sorted(line for line in result.stdout.splitlines() if line)


def build_file_index(
    target_root: str | Path,
    scope: Optional[Sequence[str]] = None,
    include_untracked: bool = False,
) -> dict:
    tracked = list_tracked_files(target_root, scope=scope)
    all_paths = list(tracked)
    if include_untracked:
        untracked = list_untracked_files(target_root)
        seen = set(all_paths)
        for relpath in untracked:
            if relpath not in seen:
                all_paths.append(relpath)
                seen.add(relpath)

    index = {}
    systems: dict = {}
    unmapped_count = 0
    for relpath in all_paths:
        system = system_for_path(relpath)
        if not system:
            unmapped_count += 1
            continue
        index[relpath] = system
        systems[system] = systems.get(system, 0) + 1

    return {
        "target_root": str(Path(target_root).resolve()),
        "index": index,
        "systems": systems,
        "file_count": len(all_paths),
        "unmapped_count": unmapped_count,
    }
