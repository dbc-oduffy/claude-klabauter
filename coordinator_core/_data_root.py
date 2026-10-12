"""_data_root.py -- where this repo's machine-local engine caches live.

``engine_data_root(env=None)`` returns ``<root>/claude-klabauter``, where ``<root>``
is the first rung yielding an absolute path:

1. ``env[ENV_OVERRIDE]`` (``env`` defaults to ``os.environ``).
2. machine-local registry key ``paths.engine_data_root`` (in-process read).
3. the per-OS user data dir, ``coordinator`` appended: ``%LOCALAPPDATA%`` on
   Windows, ``~/Library/Application Support`` on macOS, ``$XDG_DATA_HOME`` or
   ``~/.local/share`` elsewhere.

A value is ``expanduser``-ed; one that is empty or still relative is skipped, so
a relative value never yields a cwd-relative write. When no rung resolves the
call raises ``EngineDataRootUnresolved`` (an ``OSError``), which the fail-open
callers already catch. Directories are never created here.

Only the registry rung is memoized, keyed on the registry dir, both registry
files' mtimes and the process-env override; ``reset_cache`` forgets it.

Negative-spec: no subprocess, no ``machine-local`` CLI, no git, no drive literal,
and the repo segment is a constant, never derived from the checkout directory
(the published twin's checkout is named differently).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from coordinator_core import machine_resolver

REPO_SEGMENT = "claude-klabauter"
REGISTRY_KEY = "paths.engine_data_root"
ENV_OVERRIDE = "MACHINE_LOCAL_PATHS_ENGINE_DATA_ROOT"

_VENDOR_SEGMENT = "coordinator"
_UNRESOLVABLE_DIR = "<unresolvable-registry-dir>"

_cache: Dict[Tuple[Any, ...], Optional[str]] = {}


class EngineDataRootUnresolved(OSError):
    """No rung of the data-root ladder produced an absolute path."""


def reset_cache() -> None:
    """Forget the memoized registry-rung answer."""
    _cache.clear()


def _platform() -> str:
    return sys.platform


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _registry_key() -> Tuple[Any, ...]:
    try:
        reg_dir = machine_resolver.registry_dir()
    except ValueError:
        return (_UNRESOLVABLE_DIR, -1, -1, os.environ.get(ENV_OVERRIDE))
    return (
        str(reg_dir),
        _mtime(reg_dir / "registry.toml"),
        _mtime(reg_dir / "registry.local.toml"),
        os.environ.get(ENV_OVERRIDE),
    )


def _absolute(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else None


def _registry_root() -> Optional[Path]:
    key = _registry_key()
    if key not in _cache:
        _cache.clear()
        try:
            raw = machine_resolver.registry_get(REGISTRY_KEY)
        except Exception:  # noqa: BLE001 -- an unreadable registry falls through to the default
            raw = None
        _cache[key] = raw
    return _absolute(_cache[key])


def _home() -> Optional[Path]:
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return None


def _default_root(env: Mapping[str, str]) -> Optional[Path]:
    platform = _platform()
    home = _home()
    if platform == "win32":
        base = _absolute(env.get("LOCALAPPDATA"))
        if base is None and home is not None:
            base = home / "AppData" / "Local"
    elif platform == "darwin":
        base = home / "Library" / "Application Support" if home is not None else None
    else:
        base = _absolute(env.get("XDG_DATA_HOME"))
        if base is None and home is not None:
            base = home / ".local" / "share"
    return base / _VENDOR_SEGMENT if base is not None else None


def engine_data_root(env: Optional[Mapping[str, str]] = None) -> Path:
    """The machine-local engine data dir, ``<root>/claude-klabauter``.

    Raises ``EngineDataRootUnresolved`` when no rung yields an absolute path.
    """
    source = os.environ if env is None else env
    root = _absolute(source.get(ENV_OVERRIDE)) or _registry_root() or _default_root(source)
    if root is None:
        raise EngineDataRootUnresolved("no absolute engine data root resolves")
    return root / REPO_SEGMENT
