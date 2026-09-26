
from __future__ import annotations

from pathlib import Path


def _resolve_plugin_root_for_machine_local(coord_path: Path) -> Path | None:
    if not Path(coord_path).parts:
        return None
    for candidate in (coord_path / "coordinator", coord_path):
        if (candidate / "templates" / "bin" / "_machine_local.py").is_file():
            return candidate
    if (coord_path / ".claude-plugin" / "plugin.json").is_file():
        return coord_path
    return None
