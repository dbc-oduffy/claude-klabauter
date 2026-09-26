
from __future__ import annotations

from pathlib import Path
from typing import Union

from coordinator_core.machine_resolver import registry_get
from coordinator_core.win_portability import same_path

_PLANE_REGISTRY_KEYS = (
    "repos.doe_claude",
    "repos.claude_klabauter",
    "repos.claude_klabauter",
)


def session_repo_is_plane(cwd: Union[str, Path]) -> bool:
    cwd_str = str(cwd)
    for key in _PLANE_REGISTRY_KEYS:
        try:
            root = registry_get(key)
        except Exception:
            continue
        if not root:
            continue
        try:
            if same_path(cwd_str, root):
                return True
        except Exception:
            continue

    return False
