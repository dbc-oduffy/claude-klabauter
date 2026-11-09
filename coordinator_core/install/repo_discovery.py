"""Zero-spawn discovery of fleet clones by `origin` remote, for unset `repos.*` keys.

A clone is identified by the last path segment of its `[remote "origin"]` url, never by
its directory name. Reads `.git/config` and the shared registry only: no git spawn, no
network, no recursive walk (immediate children of each scan root).
"""
from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.hooks.session_start_cloud_focus import origin_slug

GENERAL_WORK_KEYS = ("repos.general_work", "coordinator.general_work_root")
_REPOS_PREFIX = "repos."


def _norm_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def remote_repo_name(clone: Path) -> str | None:
    """Repo name from `clone`'s origin url, or None (no origin; `.git` is a file)."""
    slug = origin_slug(clone)
    return slug[1] if slug else None


def declared_repo_keys() -> list[str]:
    """Root-namespace `repos.*` keys declared in the shared `registry.toml` layer."""
    import tomllib

    from coordinator_core import _settings_home
    from coordinator_core._fleet_names import _flatten
    from coordinator_core.shared_read import read_bytes_shared

    try:
        data = tomllib.loads(read_bytes_shared(_settings_home.machine_local_dir() / "registry.toml").decode("utf-8"))
    except (OSError, ValueError):
        return []
    return [k for k, _ in _flatten(data) if k.startswith(_REPOS_PREFIX)]


def wanted_for_keys(keys: list[str]) -> dict[str, str]:
    """Map each registry key to the repo name it expects (`repos.project_rag` -> `example_retrieval_repo`).

    `general-work` also feeds `coordinator.general_work_root`.
    """
    wanted = {k: k[len(_REPOS_PREFIX):] for k in keys if k.startswith(_REPOS_PREFIX)}
    if "repos.general_work" in wanted:
        wanted["coordinator.general_work_root"] = wanted["repos.general_work"]
    return wanted


def find_clones(
    scan_roots: list[Path], wanted: dict[str, str]
) -> tuple[dict[str, Path], list[str]]:
    """Return (key -> clone path, ambiguous keys).

    A key whose repo name matches two distinct clones is registered for neither and
    listed as ambiguous. Names compare lower-cased with `-` as `_`.
    """
    by_name: dict[str, list[Path]] = {}
    seen: set[str] = set()
    for root in scan_roots:
        try:
            children = sorted(p for p in Path(root).iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            ident = os.path.normcase(str(child.resolve()))
            if ident in seen or not (child / ".git").is_dir():
                continue
            seen.add(ident)
            name = remote_repo_name(child)
            if name:
                by_name.setdefault(_norm_name(name), []).append(child)
    found: dict[str, Path] = {}
    ambiguous: list[str] = []
    for key, repo in wanted.items():
        clones = by_name.get(_norm_name(repo), [])
        if len(clones) == 1:
            found[key] = clones[0]
        elif len(clones) > 1:
            ambiguous.append(key)
    return found, ambiguous


def discover_clones(scan_roots: list[Path], wanted: dict[str, str]) -> dict[str, Path]:
    """Key -> clone path for each wanted key with exactly one matching clone."""
    return find_clones(scan_roots, wanted)[0]
