"""Spawn-free resolver for the operator's real sibling-repo names.

Names come from the machine-local registry and the doctrine-repo pointer, never
from a literal, so the answer survives publish scrubbing. File reads and stats
only; an absent or unparseable source degrades to empty / None.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

_SENTINEL = ".coordinator-dev-repo"
_POINTER = "." + "doe" + "-root"
_KEY_PREFIXES = ("repos.", "engine.working_repos.")


def _flatten(node: dict, prefix: str = "") -> Iterator[tuple]:
    """Yield (dotted_key, value) for flat dotted keys and nested tables alike."""
    for key, val in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(val, dict):
            yield from _flatten(val, dotted + ".")
        else:
            yield dotted, val


def _registry_paths() -> list:
    """Raw path values under the fleet key families, registry then local overlay."""
    try:
        import tomllib

        from coordinator_core._settings_home import machine_local_dir

        reg_dir = machine_local_dir()
    except Exception:
        return []
    out = []
    for fname in ("registry.toml", "registry.local.toml"):
        try:
            with open(reg_dir / fname, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, ValueError):
            continue
        for key, val in _flatten(data):
            if key.startswith(_KEY_PREFIXES) and isinstance(val, str) and val.strip():
                out.append(val.strip())
    return out


def _leaf(raw: str) -> str:
    return Path(raw.replace("\\", "/").rstrip("/")).name


def registry_repo_names() -> tuple:
    """Basenames of every ``repos.*`` / ``engine.working_repos.*`` registry path."""
    return tuple(n for n in (_leaf(v) for v in _registry_paths()) if n)


def sibling_repo_names(default: tuple) -> tuple:
    """``default`` plus registry names, case-folded dedup, first spelling wins."""
    seen = set()
    out = []
    for name in (*default, *registry_repo_names()):
        folded = name.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(name)
    return tuple(out)


def doctrine_repo_name() -> Optional[str]:
    """Basename of the doctrine repo, or None.

    Reads the claude-home pointer first; otherwise the first registry path whose
    root carries the dev-repo sentinel.
    """
    try:
        from coordinator_core._settings_home import claude_config_dir

        raw = (claude_config_dir() / _POINTER).read_text(encoding="utf-8").strip()
        if raw and (Path(raw) / _SENTINEL).exists():
            return _leaf(raw) or None
    except Exception:
        pass
    for value in _registry_paths():
        try:
            if (Path(value) / _SENTINEL).exists():
                return _leaf(value) or None
        except OSError:
            continue
    return None
