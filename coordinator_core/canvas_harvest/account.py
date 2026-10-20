"""Resolve the claude.ai login the session runs under, for harvest and probe stamps."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _config_file() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home()
    return base / ".claude.json"


def current_login() -> str | None:
    """Return `oauthAccount.emailAddress` from `.claude.json`, or None when absent or unparseable.

    The file is read from `CLAUDE_CONFIG_DIR` when set, else the home directory. Never raises.
    """
    try:
        data = json.loads(_config_file().read_text(encoding="utf-8"))
        email = data["oauthAccount"]["emailAddress"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None
