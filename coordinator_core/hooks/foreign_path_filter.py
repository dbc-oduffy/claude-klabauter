"""
coordinator_core.hooks.foreign_path_filter — warm-door port of DoE-claude's
`coordinator/hooks/scripts/_foreign_path_filter.py`.

Purpose: the plane-repo predicate — is a session's own repo root one of the
two doctrine/engine planes (this repo's `CLAUDE.md` § Place in the fleet)?
`project_orientation.py::engine_resolution_banner` is the one live caller
this row ports.

Negative-spec (ported verbatim from the source module): no call-site
subject/incidental classification API — a single predicate,
`session_repo_is_plane(cwd) -> bool`, is the whole surface.

Shape change from the DoE source: the original resolved the registry and
compared paths through its own sibling `_engine_root.py` (a DoE-side
resolver that answers "which engine will my hooks execute" — a question
that presupposes running OUTSIDE the engine). This module runs INSIDE the
engine itself, so it reads the registry directly via
`coordinator_core.machine_resolver.registry_get` (the same dotted-key
reader `coordinator_core.root_channel_reconcile._read_registry` already
binds to) and compares paths via `coordinator_core.win_portability.
same_path` (the consolidated samefile-then-fallback primitive) instead of
re-deriving either rung. Same three `repos.*` registry keys, same fail-open
contract: an unreadable registry or an unregistered key means "not
determinably a plane repo", never a raise.

Spec backlink: DoE-claude 2026-08-30 foreign-repo-identity-suppression plan,
chunk C2/S3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from coordinator_core.machine_resolver import registry_get
from coordinator_core.win_portability import same_path

#: The `repos.*` registry keys naming the two planes: this doctrine repo, and
#: the engine plane's two own working trees (authoring vs. published-and-
#: shipped) — see this module's docstring.
_PLANE_REGISTRY_KEYS = (
    "repos.doe_claude",
    "repos.claude_klabauter",
    "repos.claude_klabauter",
)


def session_repo_is_plane(cwd: Union[str, Path]) -> bool:
    """Is `cwd` (the session's own repo root) one of the two planes?

    Fail-open to False: an unreadable registry or an unregistered key means
    "not determinably a plane repo", never a raise. Callers on the hot
    SessionStart path need a plain bool, not a tri-state.
    """
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
