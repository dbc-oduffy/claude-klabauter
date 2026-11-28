"""coordinator_core.ops.repo_declared_project_type -- the "repo.declared_project_type" op.

Read-only: reports the project_type / project_subtypes a repo's coordinator.local.md declares,
through the same per-file parse ``declared_ue`` uses. ``root`` (required, absolute) resolves to its
git toplevel by a pathlib ``.git`` walk-up (file or directory); no toplevel falls back to ``root``.
Returns ``{project_type, project_subtypes, source_file, ue}``; nothing declared is
``{None, [], None, None}``. Spawns no process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ue_project_detection import declared_project

GENERATES: list = []

_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {"root": {"type": "string"}},
    "required": ["root"],
    "additionalProperties": False,
}


def _toplevel(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


@register_op("repo.declared_project_type", params_schema=_PARAMS_SCHEMA)
def handle(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "repo.declared_project_type" handler; returns the four declared fields."""
    raw = (params or {}).get("root")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("root is required: an absolute path inside a git work tree")
    if not os.path.isabs(raw):
        raise ValueError("root must be an absolute path")
    root = Path(raw)
    if not root.is_dir():
        raise ValueError(f"root does not exist: {raw}")
    found = declared_project(_toplevel(root.resolve()))
    if found is None:
        return {"project_type": None, "project_subtypes": [], "source_file": None, "ue": None}
    return {
        "project_type": found.project_type,
        "project_subtypes": list(found.project_subtypes),
        "source_file": found.source_file.as_posix(),
        "ue": found.ue,
    }
