"""In-process reader of `*-recheck-due-*.md` markers for the workday-start step.

`recheck_due(repo_root, today, window) -> (exit_code, text)`. Dates come from the filename
only (trailing `YYYY-MM-DD.md`); no body parse, no mtime fallback, no spawn, no write. Empty
text means no marker qualifies.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Optional

_STATE_ROOT = "state"
_TASKS_ROOT = "tasks"
_DIRS: tuple[str, ...] = (_STATE_ROOT, _TASKS_ROOT)
_INFIX = "-recheck-due-"
_SUFFIX = ".md"
_DATE_LEN = len("YYYY-MM-DD")


def _name_date(name: str) -> Optional[date]:
    """The date a marker name ends with, or None when absent or not a real calendar date."""
    stem = name[: -len(_SUFFIX)]
    try:
        return date.fromisoformat(stem[-_DATE_LEN:])
    except ValueError:
        return None


def _markers(root: str, sub: str) -> list[str]:
    try:
        with os.scandir(os.path.join(root, sub)) as it:
            return [e.name for e in it if _INFIX in e.name and e.name.endswith(_SUFFIX)]
    except OSError:
        return []


def recheck_due(
    repo_root: Path, today: Optional[date] = None, window: int = 7
) -> tuple[int, str]:
    """Markers due within `window` days (overdue included), dated rows first, then undated."""
    today = today or date.today()
    root = str(repo_root)
    dated: list[tuple[date, str]] = []
    undated: list[str] = []
    for sub in _DIRS:
        for name in _markers(root, sub):
            due = _name_date(name)
            path = f"{sub}/{name}"
            if due is None:
                undated.append(path)
            elif (due - today).days <= window:
                dated.append((due, path))
    lines: list[str] = []
    for due, path in sorted(dated):
        delta = (due - today).days
        lines.append(f"{path}: due in {delta}d" if delta >= 0 else f"{path}: overdue {-delta}d")
    lines.extend(f"{p}: undated name (unparseable)" for p in sorted(undated))
    return 0, "\n".join(lines)
