"""Shared reader for the general-work scope: the registry-declared root, whether a session sits in it, and which foreign git checkout a path lies in.

No subprocess and no git spawn: callers sit on the Edit/Write hook hot path.
"""

from __future__ import annotations

import sys

from coordinator_core import machine_resolver
from coordinator_core.group_em import gem_home

_UNSET = object()


def general_work_root() -> str | None:
    """The `coordinator.general_work_root` (else `repos.general_work`) registry value; None when unset, empty, or unreadable (fail closed: no carve-out)."""
    try:
        root = machine_resolver.registry_get("coordinator.general_work_root") or machine_resolver.registry_get("repos.general_work")
    except Exception as exc:
        print(f"general_work_scope: general_work_root unreadable: {exc} (not exempting)", file=sys.stderr)
        return None
    return root if isinstance(root, str) and root else None


def in_general_work_session(cwd: str | None) -> bool:
    """True iff `cwd` is a non-empty string at or under the general-work root."""
    if not isinstance(cwd, str) or not cwd:
        return False
    root = general_work_root()
    return bool(root) and gem_home.contains(root, cwd)


def foreign_checkout(path: str, root=_UNSET) -> str | None:
    """The git checkout `path` lies in when it is not under the general-work root; None when `path` is outside every checkout or under the root."""
    if root is _UNSET:
        root = general_work_root()
    if root and gem_home.contains(root, path):
        return None
    return gem_home.inside_checkout(path)
