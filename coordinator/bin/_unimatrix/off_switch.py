from __future__ import annotations

import os
from pathlib import Path


def marker() -> Path:
    home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if not home:
        home = str(Path(os.environ.get("CLAUDE_HOME") or Path.home()) / ".coordinator-claude-settings")
    return Path(home) / "unimatrix" / "DISABLED"


def is_off() -> bool:
    return marker().exists()
