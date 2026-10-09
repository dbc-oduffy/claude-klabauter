"""coordinator_core.ops._research_local — the local-only research root, shared by the
research emit route and research.close.

Purpose: a local-only run (vendor-proprietary code whose research may enter no repo) keeps
its scratch dir and its archive under the machine-local ``research.local_root``. That root,
and every path resolved under it, must sit outside every git checkout: a path inside one is
one `git add` away from a commit.

Negative-spec: reads the registry and walks parents for ``.git``; spawns nothing, writes
nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

LOCAL_ROOT_KEY = "research.local_root"


class LocalRootError(ValueError):
    """The local-only root is unset, relative, or inside a git checkout."""


def enclosing_checkout(path: Path) -> Optional[Path]:
    """The git checkout containing ``path`` (its own or an ancestor's ``.git``), else None."""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def outside_checkouts(path: Path, what: str) -> Path:
    """``path`` resolved, refused when it is relative or inside a git checkout."""
    if not path.is_absolute():
        raise LocalRootError(f"{what} must be absolute: {path.as_posix()!r}")
    checkout = enclosing_checkout(path)
    if checkout is not None:
        raise LocalRootError(
            f"{what} {path.as_posix()!r} is inside the git checkout {checkout.as_posix()!r}; "
            "local-only research may enter no repo"
        )
    return path.resolve()


def local_root() -> Path:
    """The machine-local ``research.local_root``, validated outside every checkout."""
    from coordinator_core.machine_resolver import registry_get

    raw = registry_get(LOCAL_ROOT_KEY)
    if not raw:
        raise LocalRootError(
            f"{LOCAL_ROOT_KEY} is unset: machine-local set {LOCAL_ROOT_KEY} <dir outside every "
            "git checkout>"
        )
    return outside_checkouts(Path(raw), LOCAL_ROOT_KEY)


def under_local_root(path: Path, root: Path, what: str) -> Path:
    """``path`` (absolute, or relative to ``root``) resolved, refused unless under ``root``."""
    candidate = (path if path.is_absolute() else root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise LocalRootError(f"{what} {candidate.as_posix()!r} is not under {root.as_posix()!r}")
    return candidate
