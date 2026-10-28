"""coordinator_core.ops.cache_caps -- size/age caps for regenerable caches.

One table row per regenerable cache class; `apply_caps` deletes files from a
class's root until it is inside its caps (age first, then oldest-first down to the
size cap). Defaults are overridable through machine-local
`cache_caps.<class>.max_mb` / `cache_caps.<class>.max_age_hours`
(`claude_home_census.cache_cap_key`).

Only files are deleted, never the class root, and nothing outside a listed root is
touched. Toolchain caches (uv, pip, npm, pnpm, huggingface) are reclaimed by
`cruft_sweep.sweep_toolchain_caches` and are not duplicated here. Transcripts and
`<tempdir>/claude` are the retention rule's (`cruft_sweep`, `prune_claude_temp`).

Not capped, with the writer and the reason:

* `~/.claude/file-history` -- Claude Code; backs rewind and is not rebuildable.
* `~/.claude/plugins/cache` -- the harness's plugin manager; it re-fetches on its own
  schedule and a partial delete breaks installed plugins.
* `~/.claude/session-env`, `~/.claude/logs` -- Claude Code; live-session state/audit.
* settings-home `example-retrieval-repo/` -- example-retrieval-repo's own index home; bounded by the
  example-retrieval-repo repo, never written from claude-klabauter.
* settings-home `state/`, `machine-local/`, `group-em/` -- claude-klabauter engine state,
  per-box config and group-em records: authoritative, not derived.
* settings-home `telemetry/`, `install/`, `secrets/`, `bin/`, `claude-klabauter/`,
  `coordinator-claude/`, `retired-shims/` -- installer/publish artifacts and
  credentials; not regenerable by a sweep.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from coordinator_core.ops import claude_home_census as census
from coordinator_core.ops.cruft_sweep import _delete_file

GENERATES = []

MB = 1024 * 1024
HOUR = 3600
PROTECTED_TOP_LEVEL = "project-rag"


@dataclass(frozen=True)
class CacheClass:
    """One regenerable cache. `base` is `claude_home` or `settings_home`; `rel` is
    the subdir under it. A None cap is not applied."""

    name: str
    base: str
    rel: str
    max_mb: Optional[float]
    max_age_hours: Optional[float]


CACHE_CLASSES: Sequence[CacheClass] = (
    CacheClass("debug", "claude_home", "debug", 200, 72),
    CacheClass("shell-snapshots", "claude_home", "shell-snapshots", 50, 72),
    CacheClass("todos", "claude_home", "todos", 50, 168),
    CacheClass("paste-cache", "claude_home", "paste-cache", 100, 72),
    CacheClass("cache", "claude_home", "cache", 500, 168),
    CacheClass("settings-cache", "settings_home", "cache", 1024, 168),
    CacheClass("settings-scratch", "settings_home", "scratch", 1024, 168),
    CacheClass("heavy-admission", "settings_home", "heavy-admission", None, 24),
    CacheClass("tier-t-slots", "settings_home", "tier-t-slots", None, 24),
    CacheClass("taskstop-reaper", "settings_home", "taskstop-reaper", None, 24),
    # Warm engine stderr dirs resolve per clone hash (see `_warm_roots`).
    CacheClass("pool-worker-stderr", "warm", "pool-worker-stderr", 50, 72),
)


def _registry_get(key: str) -> Optional[str]:
    return census._registry_get(key)


def resolve_caps(cls: CacheClass, get: Callable[[str], Optional[str]] = _registry_get) -> "tuple[Optional[int], Optional[float]]":
    """(max_bytes, max_age_seconds) for a class, machine-local overrides applied.
    A non-positive or unparseable override falls back to the table default."""
    mb = census._positive_number(get(census.cache_cap_key(cls.name, "max_mb")), cls.max_mb or 0) or None
    hours = census._positive_number(get(census.cache_cap_key(cls.name, "max_age_hours")), cls.max_age_hours or 0) or None
    return (int(mb * MB) if mb else None, hours * HOUR if hours else None)


def _files(root: Path) -> List["tuple[float, int, str]"]:
    """(mtime, size, path) for every regular file under root; symlinks skipped."""
    out: List["tuple[float, int, str]"] = []
    stack = [os.fspath(root)] if root.is_dir() else []
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            out.append((st.st_mtime, st.st_size, entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def cap_root(root: Path, max_bytes: Optional[int], max_age_seconds: Optional[float],
             *, apply: bool, now: Optional[float] = None) -> dict:
    """Bring one root inside its caps. Deletes expired files, then oldest-first until
    the remainder fits `max_bytes`. Dry-run (`apply=False`) reports without deleting."""
    now = time.time() if now is None else now
    files = sorted(_files(root))
    total = sum(size for _, size, _ in files)
    doomed: List["tuple[float, int, str]"] = []
    kept: List["tuple[float, int, str]"] = []
    for item in files:
        if max_age_seconds is not None and now - item[0] > max_age_seconds:
            doomed.append(item)
        else:
            kept.append(item)
    remaining = sum(size for _, size, _ in kept)
    if max_bytes is not None:
        while kept and remaining > max_bytes:
            item = kept.pop(0)
            doomed.append(item)
            remaining -= item[1]
    deleted = freed = 0
    for _, size, path in doomed:
        if not apply or _delete_file(Path(path)):
            deleted += 1
            freed += size
    return {"root": str(root), "bytes_before": total, "files_deleted": deleted,
            "bytes_freed": freed, "applied": apply}


def _warm_roots() -> List[Path]:
    return census.warm_stderr_dirs()


def _roots_for(cls: CacheClass, claude_home: Path, settings_home: Optional[Path],
               warm_roots: Optional[Sequence[Path]]) -> List[Path]:
    if cls.base == "claude_home":
        return [claude_home / cls.rel]
    if cls.base == "settings_home":
        if settings_home is None or cls.rel == PROTECTED_TOP_LEVEL:
            return []
        return [settings_home / cls.rel]
    if cls.base == "warm":
        return list(warm_roots) if warm_roots is not None else _warm_roots()
    return []


def apply_caps(claude_home: Optional[Path] = None, settings_home: Optional[Path] = None,
               *, apply: bool = True, now: Optional[float] = None,
               get: Callable[[str], Optional[str]] = _registry_get,
               warm_roots: Optional[Sequence[Path]] = None,
               classes: Sequence[CacheClass] = CACHE_CLASSES) -> List[Dict[str, object]]:
    """Cap every class in the table. `claude_home` defaults to the Claude home and
    `settings_home` to the settings home. Returns one result row per root."""
    claude_home = claude_home if claude_home is not None else census.claude_home()
    if settings_home is None:
        try:
            from coordinator_core._settings_home import settings_home as _sh
            settings_home = Path(_sh())
        except Exception:  # noqa: BLE001 -- no settings home: skip its rows
            settings_home = None
    rows: List[Dict[str, object]] = []
    for cls in classes:
        max_bytes, max_age = resolve_caps(cls, get)
        for root in _roots_for(cls, claude_home, settings_home, warm_roots):
            row = cap_root(root, max_bytes, max_age, apply=apply, now=now)
            row["class"] = cls.name
            rows.append(row)
    return rows
