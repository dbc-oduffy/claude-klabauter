"""Refresh-log baseline reads shared by the drift op and the orient brief.

Import-light by contract: the orient brief's import closure must not reach
`coordinator_core.ipc`, so nothing here may import `drift` or register an op.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

_PROG = "check-plugin-drift"


def resolve_claude_home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home())) / ".claude"


def resolve_refresh_log() -> Path:
    return resolve_claude_home() / "plugins" / ".refresh-log"


def pyproject_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def refresh_log_baseline_hash(refresh_log: Path, plugin_name: str) -> str:
    """Last `pyproject_hash=` recorded for `plugin_name` in the refresh audit log."""
    if not refresh_log.exists():
        return ""
    try:
        text = refresh_log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[warn] {_PROG}: could not read {refresh_log}: {exc}", file=sys.stderr)
        return ""
    baseline = ""
    for line in text.splitlines():
        if f" {plugin_name} " not in line or "pyproject_hash=" not in line:
            continue
        m = re.search(r"pyproject_hash=([a-f0-9]*)", line)
        if m:
            baseline = m.group(1)
    return baseline.replace("\r", "")
