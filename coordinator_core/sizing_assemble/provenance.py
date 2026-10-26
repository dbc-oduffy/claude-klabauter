"""Provenance stamp for sizing records: which engine and DoE plugin produced them."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.engine_version import engine_build


def _read_version(root: Path) -> str | None:
    for rel in (".claude-plugin/plugin.json", "plugin.json"):
        p = root / rel
        if p.is_file():
            try:
                version = json.loads(p.read_text(encoding="utf-8")).get("version")
            except (OSError, ValueError):
                continue
            if isinstance(version, str) and version:
                return version
    return None


def resolve_doe_plugin_sha() -> str | None:
    """Loaded DoE plugin content root's HEAD sha, else its plugin version string, else None. Never raises."""
    try:
        from coordinator_core.resolve_coordinator_clone import resolve_content_root_spawn_free
        from coordinator_core.git.git_state import head_sha

        resolved = resolve_content_root_spawn_free()
        if not resolved:
            return None
        root = Path(resolved)
        return head_sha(root) or _read_version(root)
    except Exception:
        return None


def stamp() -> dict:
    """`{engine_sha, engine_dirty, doe_plugin_sha}`; 0 spawns. `engine_dirty` is always None
    (see `engine_build`). Trap: doe_plugin_sha may hold a plugin version string, not a sha,
    when the content root has no readable `.git` HEAD."""
    return {**engine_build(), "doe_plugin_sha": resolve_doe_plugin_sha()}
