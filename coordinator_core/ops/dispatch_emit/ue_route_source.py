"""UE repo detection and the installed example-game-repo-control route-table source for agent routing.

Stat-only: os.scandir and file existence checks, no subprocess, no git. Results are memoised per
process; `clear_caches()` resets them.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.git.git_objects import PACK_READ_DEADLINE_SECS as READ_DEADLINE_SECS
from coordinator_core.ops.dispatch_emit.plugin_review_overlay import (
    PLUGIN_ROOTS_ENV,
    _plugin_name,
    installed_plugin_roots,
)
from coordinator_core.ue_project_detection import SKIP_SEGMENTS, declared_ue

EXAMPLE_GAME_REPO_PLUGIN = "example-game-repo-control"
MANIFEST_RELPATHS = ("routing/ue-job-shapes.json", "route-table.json")
_UE_SUFFIXES = (".uproject", ".uplugin")
_MAX_DEPTH = 3
# Never home to a .uproject/.uplugin, and in a coordinator repo they hold nearly every entry:
# walking them cost example-retrieval-repo 6.4 s cold after a warm reload (ask_stage hang, 2026-10-31).
_WALK_SKIP = SKIP_SEGMENTS | {
    "scratch", "tasks", "archive", "node_modules", "state", "dist",
    "Binaries", "Intermediate", "Saved", "DerivedDataCache",
}

_clock = time.monotonic
_UE_REPO_CACHE: dict[str, bool] = {}
_TABLE_PATH_CACHE: dict[tuple[str, str], Optional[Path]] = {}
_ROSTER_CACHE: dict[str, frozenset[str]] = {}


def clear_caches() -> None:
    _UE_REPO_CACHE.clear()
    _TABLE_PATH_CACHE.clear()
    _ROSTER_CACHE.clear()


# A UE project root is a handful of entries; a directory past this is a data tree (a photo
# corpus wedged the warm engine for every peer, 2026-11-02), so it is neither read past the cap
# nor descended. The total cap bounds a wide tree of small directories the same way.
_MAX_DIR_ENTRIES = 256
_MAX_TOTAL_ENTRIES = 4096


def _has_ue_marker(dir_path: str, depth: int, deadline: Optional[float] = None,
                   budget: Optional[list] = None) -> bool:
    """Marker walk bounded by depth, per-directory and total entry caps, and `deadline`
    (monotonic); whatever lies past a bound reads as no marker."""
    if deadline is None:
        deadline = _clock() + READ_DEADLINE_SECS
    elif _clock() >= deadline:
        return False
    if budget is None:
        budget = [_MAX_TOTAL_ENTRIES]
    subdirs: list[str] = []
    seen = 0
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                seen += 1
                budget[0] -= 1
                if seen > _MAX_DIR_ENTRIES or budget[0] < 0:
                    return False
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if not entry.name.startswith(".") and entry.name not in _WALK_SKIP:
                        subdirs.append(entry.path)
                elif entry.name.endswith(_UE_SUFFIXES):
                    return True
    except OSError:
        return False
    if depth < _MAX_DEPTH:
        return any(_has_ue_marker(sub, depth + 1, deadline, budget) for sub in sorted(subdirs))
    return False


def is_ue_repo(repo_root: str | os.PathLike[str]) -> bool:
    """True when `repo_root`'s coordinator.local.md declares UE, or a `*.uproject`/`*.uplugin` sits within depth 3 (dot and `_WALK_SKIP` dirs excluded)."""
    key = os.path.abspath(os.fspath(repo_root))
    cached = _UE_REPO_CACHE.get(key)
    if cached is None:
        cached = _UE_REPO_CACHE[key] = declared_ue(key) is True or _has_ue_marker(key, 0)
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
