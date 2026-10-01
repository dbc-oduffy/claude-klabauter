"""Repo standing: registered / onboarded / install-clone, resolved with zero spawns.

`install_clone` is true for a path under the plugin cache, or for the published
engine mirror on a consumer-profile box. An author box's claude-klabauter checkout is never
an install clone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from coordinator_core import _settings_home, engine_root, machine_profile, machine_resolver
from coordinator_core.win_portability import same_path


@dataclass(frozen=True)
class RepoStanding:
    path: str
    registered_key: Optional[str]
    onboarded: bool
    install_clone: bool


def is_onboarded(repo_root: str | os.PathLike) -> bool:
    """True when `archive/` or `state/workstreams/` exists.

    `state/workstreams/` alone is unsound: it is created lazily on the first
    workstream event, and most onboarded repos lack it. `archive/` is the arm
    `completion_archive_predicate` also keys on.
    """
    root = os.fspath(repo_root)
    return os.path.isdir(os.path.join(root, "archive")) or os.path.isdir(
        os.path.join(root, "state", "workstreams")
    )


def plugin_cache_root() -> Path:
    return _settings_home.claude_config_dir() / "plugins" / "cache"


def _norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_within(path: str, root: str) -> bool:
    # same_path on the resolved common parent: containment, never a string prefix.
    try:
        common = os.path.commonpath([_norm(path), _norm(root)])
    except ValueError:
        return False
    return same_path(common, root)


def _registered_key(path: str) -> Optional[str]:
    try:
        flat = machine_resolver.merged_flat_registry()
        repos = {
            k: v for k, v in flat.items()
            if k.startswith("repos.") and isinstance(v, str) and v
        }
        return machine_resolver.canonical_repo_key_for_root(path, repos)
    except Exception:  # noqa: BLE001 -- unreadable registry degrades to not registered
        return None


def _install_clone(path: str) -> bool:
    try:
        if _is_within(path, str(plugin_cache_root())):
            return True
    except Exception:  # noqa: BLE001 -- unresolvable claude home: no cache arm
        pass
    try:
        return machine_profile.machine_profile() == "consumer" and engine_root.is_published_engine_mirror(path)
    except Exception:  # noqa: BLE001
        return False


def repo_standing(path: str | os.PathLike) -> RepoStanding:
    p = _norm(path)
    return RepoStanding(
        path=p,
        registered_key=_registered_key(p),
        onboarded=is_onboarded(p),
        install_clone=_install_clone(p),
    )


def is_install_clone(path: str | os.PathLike) -> bool:
    return repo_standing(path).install_clone
