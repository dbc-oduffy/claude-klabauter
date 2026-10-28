"""Plugin review-signal overlay reader for ask compose: `{signal: selects}` from the `review-signals.json`
each installed plugin ships, read in-process (no spawn). Mirrors DoE `lib/plugin_review_signals.py scan`:
a malformed declaration or an invalid entry is skipped, never raised; base signal ids are never shadowed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional

DECLARATION = "review-signals.json"
SCHEMA_NAME = "plugin-review-signals"
PLUGIN_ROOTS_ENV = "COORDINATOR_PLUGIN_ROOTS"
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME") or Path.home() / ".claude")


def _fallback_root(key: str, install: Path, home: Path) -> Optional[Path]:
    """A real directory for a dangling `installPath`: the path minus its `cache` segment, else
    `<home>/plugins/<marketplace>/<plugin>` from the `plugin@marketplace` key."""
    tries = []
    if "cache" in install.parts:
        tries.append(Path(*[p for p in install.parts if p != "cache"]))
    name, _, market = key.partition("@")
    if name and market:
        tries.append(home / "plugins" / market / name)
    return next((t for t in tries if t.is_dir()), None)


def installed_plugin_roots(home: Optional[Path] = None, extra: Iterable[str] = ()) -> list[Path]:
    """`extra`, `$COORDINATOR_PLUGIN_ROOTS`, then `installed_plugins.json` install paths; deduplicated existing dirs."""
    home = home or _claude_home()
    candidates = [Path(p) for p in extra]
    candidates += [Path(p) for p in os.environ.get(PLUGIN_ROOTS_ENV, "").split(os.pathsep) if p.strip()]
    try:
        data = json.loads((home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        for key, installs in (data.get("plugins") or {}).items():
            for install in installs if isinstance(installs, list) else []:
                if not (isinstance(install, dict) and install.get("installPath")):
                    continue
                path = Path(install["installPath"])
                real = path if path.is_dir() else _fallback_root(str(key), path, home)
                if real is not None:
                    candidates.append(real)
    except (OSError, ValueError, AttributeError):
        pass
    seen: set[str] = set()
    roots: list[Path] = []
    for c in candidates:
        if not c.is_dir():
            continue
        try:
            key = str(c.resolve())
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            roots.append(c)
    return roots


def _plugin_name(root: Path) -> str:
    try:
        name = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, ValueError, AttributeError):
        pass
    return root.name


def plugin_signal_selects(
    reserved: Iterable[str] = (), roots: Optional[Iterable[Path]] = None, home: Optional[Path] = None
) -> dict[str, str]:
    """`{signal id: selects}` contributed by installed plugins, one pass over their declarations.

    A signal whose id is kebab-invalid, in `reserved`, already taken, or whose `selects` is outside the
    declaring plugin's `<plugin>:` namespace is skipped.
    """
    taken = set(reserved)
    out: dict[str, str] = {}
    for root in installed_plugin_roots(home) if roots is None else roots:
        decl = Path(root) / DECLARATION
        try:
            doc = json.loads(decl.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_NAME or not isinstance(doc.get("signals"), list):
            continue
        plugin = _plugin_name(Path(root))
        for sig in doc["signals"]:
            if not isinstance(sig, dict):
                continue
            sid, selects = sig.get("id"), sig.get("selects")
            if not (isinstance(sid, str) and _KEBAB.match(sid)) or sid in taken:
                continue
            if not (isinstance(selects, str) and selects.startswith(f"{plugin}:") and len(selects) > len(plugin) + 1):
                continue
            taken.add(sid)
            out[sid] = selects
    return out
