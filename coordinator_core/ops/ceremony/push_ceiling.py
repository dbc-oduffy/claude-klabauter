"""Per-repo push ceiling resolver, zero spawns.

`resolve_push_ceiling` answers "how long may THIS repo's push ladder run?" from file
reads only. Must not import `push_outstanding` (it imports this module).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple, Union

from coordinator_core.ops.ceremony.push import _read_git_config_text

CONFIG_KEY = "coordinator.pushCeilingSecs"

#: Floor for a repo whose LFS filter is declared and required and that sets no explicit key.
LFS_PUSH_CEILING_FLOOR_SECS: float = 60.0

#: Total-duration backstop above the silence watchdog (STALL_SILENCE_SECS): no resolved
#: ceiling exceeds it, so a progressing push cannot run unbounded.
PUSH_CEILING_MAX_SECS: float = 120.0

_LFS_FILTER_MARKER = "filter=lfs"
_TRUTHY = frozenset({"true", "yes", "on", "1"})

#: Keyed by the env that selects the global files, so a changed HOME/GIT_CONFIG_GLOBAL
#: re-reads; the repo-local file is never cached.
_GLOBAL_MEMO: "dict[Tuple[str, str, str], bool]" = {}


def gitattributes_declares_lfs_filter(root: Path) -> bool:
    """Zero-spawn: does *root*'s root `.gitattributes` declare `filter=lfs` (comments
    stripped)? `False` when the file is absent, empty, or unreadable."""
    try:
        text = (Path(root) / ".gitattributes").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        if _LFS_FILTER_MARKER in code:
            return True
    return False


def _scan(text: str, section: str, subsection: Optional[str], key: str) -> Optional[str]:
    """Last value of `key` under `[section]`/`[section "subsection"]`, else None."""
    found: Optional[str] = None
    active = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            close = line.find("]")
            if close == -1:
                active = False
                continue
            header = line[1:close].strip()
            parts = header.split(None, 1)
            name = parts[0].lower() if parts else ""
            sub = parts[1].strip() if len(parts) > 1 else None
            if sub is not None and len(sub) >= 2 and sub[0] == '"' and sub[-1] == '"':
                sub = sub[1:-1]
            active = name == section and sub == subsection
            continue
        if not active or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip().lower() != key.lower():
            continue
        v = v.strip()
        for marker in (" #", " ;", "\t#", "\t;"):
            cut = v.find(marker)
            if cut != -1:
                v = v[:cut].rstrip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        found = v
    return found


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _global_candidates() -> List[Path]:
    candidates: List[Path] = [Path("/etc/gitconfig")]
    override = os.environ.get("GIT_CONFIG_GLOBAL")
    if override:
        candidates.append(Path(override))
        return candidates
    try:
        home: Optional[Path] = Path(os.path.expanduser("~"))
    except Exception:  # noqa: BLE001
        home = None
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "git" / "config")
    elif home is not None:
        candidates.append(home / ".config" / "git" / "config")
    if home is not None:
        candidates.append(home / ".gitconfig")
    return candidates


def _lfs_required(text: str) -> Optional[bool]:
    value = _scan(text, "filter", "lfs", "required")
    if value is None:
        return None
    return value.strip().lower() in _TRUTHY


def _global_lfs_required() -> bool:
    memo_key = (
        os.environ.get("GIT_CONFIG_GLOBAL", ""),
        os.environ.get("XDG_CONFIG_HOME", ""),
        os.path.expanduser("~"),
    )
    cached = _GLOBAL_MEMO.get(memo_key)
    if cached is not None:
        return cached
    required = False
    for path in _global_candidates():
        verdict = _lfs_required(_read(path))
        if verdict is not None:
            required = verdict
    _GLOBAL_MEMO[memo_key] = required
    return required


def _explicit_ceiling(text: str) -> Optional[float]:
    raw = _scan(text, "coordinator", None, "pushCeilingSecs")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value or value <= 0 or value == float("inf"):
        return None
    return value


def resolve_push_ceiling(root: Union[Path, str], *, default_secs: float) -> float:
    """The push-ladder ceiling for the repo at *root*, in seconds. Zero spawns.

    Order: explicit `coordinator.pushCeilingSecs` in the common-dir config, clamped to
    `[default_secs, PUSH_CEILING_MAX_SECS]`; else `max(default_secs,
    LFS_PUSH_CEILING_FLOOR_SECS)` when root `.gitattributes` declares `filter=lfs` and
    `filter.lfs.required` is true in any scope; else `default_secs`. A `default_secs`
    above the maximum is returned as is.
    """
    root_path = Path(root)
    ceiling_cap = max(PUSH_CEILING_MAX_SECS, default_secs)
    try:
        text = _read_git_config_text(root_path)
    except RuntimeError:  # git_common_dir raises outside a repo
        text = ""
    explicit = _explicit_ceiling(text)
    if explicit is not None:
        return min(max(explicit, default_secs), ceiling_cap)
    if gitattributes_declares_lfs_filter(root_path):
        local = _lfs_required(text)
        required = local if local is not None else _global_lfs_required()
        if required:
            return min(max(default_secs, LFS_PUSH_CEILING_FLOOR_SECS), ceiling_cap)
    return default_secs
