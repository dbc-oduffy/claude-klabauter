"""UE repo detection and the installed example-game-repo-control route-table source for agent routing.

Stat-only: os.scandir and file existence checks, no subprocess, no git. Results are memoised per
process; `clear_caches()` resets them.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.plugin_review_overlay import (
    PLUGIN_ROOTS_ENV,
    _plugin_name,
    installed_plugin_roots,
)
from coordinator_core.ue_project_detection import SKIP_SEGMENTS

EXAMPLE_GAME_REPO_PLUGIN = "example-game-repo-control"
MANIFEST_RELPATHS = ("routing/ue-job-shapes.json", "route-table.json")
_UE_SUFFIXES = (".uproject", ".uplugin")
_MAX_DEPTH = 3
# Never home to a .uproject/.uplugin, and in a coordinator repo they hold nearly every entry:
# walking them cost example-retrieval-repo 6.4 s cold after a warm reload (ask_stage hang, 2026-10-31).
_WALK_SKIP = SKIP_SEGMENTS | {
    "scratch", "state", "tasks", "archive", "node_modules", "dist",
    "Binaries", "Intermediate", "Saved", "DerivedDataCache",
}

_UE_REPO_CACHE: dict[str, bool] = {}
_TABLE_PATH_CACHE: dict[tuple[str, str], Optional[Path]] = {}
_ROSTER_CACHE: dict[str, frozenset[str]] = {}


def clear_caches() -> None:
    _UE_REPO_CACHE.clear()
    _TABLE_PATH_CACHE.clear()
    _ROSTER_CACHE.clear()


def _has_ue_marker(dir_path: str, depth: int) -> bool:
    try:
        entries = list(os.scandir(dir_path))
    except OSError:
        return False
    subdirs: list[str] = []
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            if not entry.name.startswith(".") and entry.name not in _WALK_SKIP:
                subdirs.append(entry.path)
        elif entry.name.endswith(_UE_SUFFIXES):
            return True
    if depth < _MAX_DEPTH:
        return any(_has_ue_marker(sub, depth + 1) for sub in sorted(subdirs))
    return False


def is_ue_repo(repo_root: str | os.PathLike[str]) -> bool:
    """True when a `*.uproject` or `*.uplugin` sits within depth 3 of `repo_root` (dot and `_WALK_SKIP` dirs excluded)."""
    key = os.path.abspath(os.fspath(repo_root))
    cached = _UE_REPO_CACHE.get(key)
    if cached is None:
        cached = _UE_REPO_CACHE[key] = _has_ue_marker(key, 0)
    return cached


def example_game_repo_route_table_path() -> Optional[Path]:
    """Manifest file of the first installed example-game-repo-control plugin root, else None.

    Per root, `routing/ue-job-shapes.json` is checked before `route-table.json`.
    """
    memo_key = (os.environ.get(PLUGIN_ROOTS_ENV, ""), os.environ.get("CLAUDE_HOME", ""))
    if memo_key in _TABLE_PATH_CACHE:
        return _TABLE_PATH_CACHE[memo_key]
    found: Optional[Path] = None
    for root in installed_plugin_roots():
        if _plugin_name(root) != EXAMPLE_GAME_REPO_PLUGIN:
            continue
        found = next((root / rel for rel in MANIFEST_RELPATHS if (root / rel).is_file()), None)
        if found is not None:
            break
    _TABLE_PATH_CACHE[memo_key] = found
    return found


def example_game_repo_roster(root: str | os.PathLike[str]) -> frozenset[str]:
    """`example-game-repo-control:<stem>` for each `agents/*.md` under the install `root`."""
    key = os.path.abspath(os.fspath(root))
    cached = _ROSTER_CACHE.get(key)
    if cached is None:
        names: set[str] = set()
        try:
            for entry in os.scandir(os.path.join(key, "agents")):
                if entry.name.endswith(".md") and entry.is_file():
                    names.add(f"{EXAMPLE_GAME_REPO_PLUGIN}:{entry.name[:-3]}")
        except OSError:
            pass
        cached = _ROSTER_CACHE[key] = frozenset(names)
    return cached
