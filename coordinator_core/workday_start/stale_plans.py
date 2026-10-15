"""In-process workday-start step: `status: executing` plans untouched for more than 3 days.

`stale_executing_plans(repo_root) -> (exit_code, text)`; the runner calls it as an `inproc`
step. Read only: no spawn, no write. Empty text means no plan is stale. "Untouched" is the plan
file's mtime, not its last git commit (REQ-W1), so a freshly checked-out tree reads as fresh.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

PLANS_DIR = "docs/plans"
STALE_AFTER_DAYS = 3
_HEAD_CAP = 65536
_STATUS = re.compile(r"^status:[ \t]*['\"]?([^\s'\"#]*)['\"]?[ \t]*(?:#.*)?$")


def _frontmatter_status(path: str) -> Optional[str]:
    """The `status:` inside the leading `---` block, or None.

    Reads at most _HEAD_CAP bytes; a block that never closes within the cap yields None.
    """
    try:
        with open(path, "rb") as fh:
            buf = fh.read(_HEAD_CAP)
    except OSError:
        return None
    lines = buf.decode("utf-8", errors="replace").split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    for line in lines[1:]:
        line = line.rstrip("\r")
        if line == "---":
            return None
        m = _STATUS.match(line)
        if m:
            return m.group(1)
    return None


def stale_executing_plans(repo_root: Path, now: Optional[float] = None) -> tuple[int, str]:
    """One `  - <path> (status: executing, untouched <N>d)` line per stale executing plan.

    Sorted by path; N is whole days of mtime age; the threshold is strictly more than
    STALE_AFTER_DAYS. `now` is epoch seconds, injectable for tests.
    """
    directory = os.path.join(str(repo_root), *PLANS_DIR.split("/"))
    current = time.time() if now is None else now
    lines: list[str] = []
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return 0, ""
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        try:
            if not entry.is_file():
                continue
            age_days = (current - entry.stat().st_mtime) / 86400.0
        except OSError:
            continue
        if age_days <= STALE_AFTER_DAYS:
            continue
        if _frontmatter_status(entry.path) != "executing":
            continue
        lines.append(f"  - {PLANS_DIR}/{entry.name} (status: executing, untouched {int(age_days)}d)")
    return 0, "\n".join(lines)
