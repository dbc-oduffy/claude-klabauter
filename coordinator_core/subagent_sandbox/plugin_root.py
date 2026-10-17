"""Light leaf resolver for the coordinator-claude plugin content root.

`provision_report.resolve_plugin_root` delegates here; the warm client calls it
directly so a child warm reach does not import provision_report's module closure.
Resolution order and the `snippets/` probe are documented on the delegating function.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

from coordinator_core._settings_home import claude_config_dir

__all__ = ["resolve_plugin_root"]


def resolve_plugin_root(
    *,
    config_dir: Callable[[], Path] = claude_config_dir,
    content_root_reader: Optional[Callable[[], str]] = None,
) -> Optional[str]:
    """Return the plugin content root (the dir holding `snippets/`), or None on a miss.

    `config_dir` and `content_root_reader` are injection seams for the delegating
    wrapper; `content_root_reader` is imported only when rungs 1-3 miss.
    """
    def _has_content(candidate: Path) -> bool:
        return (candidate / "snippets").is_dir()

    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        candidate = Path(env_root)
        if _has_content(candidate):
            return str(candidate)

    plugin_base = config_dir() / "plugins" / "coordinator-claude"
    for candidate in (plugin_base / "coordinator", plugin_base):
        if _has_content(candidate):
            return str(candidate)

    # The harness's own install registry: a cloud or marketplace install records the plugin's
    # real `installPath`, which need not sit under `plugins/coordinator-claude` at all.
    try:
        registry = json.loads(
            (config_dir() / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
        )
        entries = registry.get("plugins", {}) if isinstance(registry, dict) else {}
    except (OSError, ValueError):
        entries = {}
    for key, installs in entries.items():
        if not key.startswith("coordinator@") or not isinstance(installs, list):
            continue
        for install in installs:
            install_path = install.get("installPath") if isinstance(install, dict) else None
            if not install_path:
                continue
            for candidate in (Path(install_path) / "coordinator", Path(install_path)):
                if _has_content(candidate):
                    return str(candidate)

    if content_root_reader is None:
        from coordinator_core.content_root import read_content_root as content_root_reader
    content_root = content_root_reader().strip()
    if content_root:
        from coordinator_core.data_root import content_root_for

        candidate = content_root_for(content_root)
        if candidate is not None and _has_content(candidate):
            return str(candidate)

    return None
