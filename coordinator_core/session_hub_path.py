"""Leaf resolver for the per-session state hub.

A normal repo's hub lives at ``<main checkout>/.coordinator-local/coordinator-sessions``
(outside ``.git``, which a sandboxed child cannot write); a bare repo keeps
``<common dir>/coordinator-sessions``. Mirrors ``locked_write._lock_dir_path``.

Leaf module: stdlib only at import time, never imports ``session.core`` (the
hooks hot path must not pay it). Zero spawns on the path-math functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

HUB_NAME = "coordinator-sessions"

GENERATES: list = []


def hub_for_common(common: Path) -> Path:
    """Hub directory for a git common dir, WITHOUT creating it."""
    common = Path(common)
    if common.name == ".git":
        return common.parent / ".coordinator-local" / HUB_NAME
    return common / HUB_NAME


def session_dir_for(common: Path, sid: str) -> Path:
    """``<hub>/<sid>`` for a git common dir, WITHOUT creating it."""
    return hub_for_common(common) / sid


def hub_for_root(cwd: Optional[str] = None) -> Optional[Path]:
    """Hub for the repo containing *cwd*; ``None`` outside git."""
    from coordinator_core.git.repo_root import git_common_dir

    common = git_common_dir(cwd)
    if not common:
        return None
    return hub_for_common(Path(common))
