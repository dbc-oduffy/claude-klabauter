"""In-process workday-start step: `status: draft` memos left unsent past the stale window.

`stale_outbox_drafts(repo_root) -> (exit_code, text)`; the runner calls it as an `inproc` step.
Read only: no spawn, no write. Empty text means no draft root, no draft, or every draft fresh.
Staleness is the file mtime, so a draft edited again restarts its clock. The sent-memo outbox
(`state/cross-repo/outbox`) is not a draft root and is never read. The lost-delivery leg is
not built here: it measured over the bar in its spike.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.session.machinery_paths import (
    LEGACY_MEMO_OUTBOX_RELDIR,
    MEMO_OUTBOX_RELDIR,
)

STALE_HOURS_ENV = "COORDINATOR_OUTBOX_STALE_HOURS"
DEFAULT_STALE_HOURS = 24.0
OFFER = "send | compose | discard"
_HEAD_CAP = 4096
_ROOTS = (MEMO_OUTBOX_RELDIR, LEGACY_MEMO_OUTBOX_RELDIR)


def _stale_hours() -> float:
    raw = os.environ.get(STALE_HOURS_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STALE_HOURS
    return value if value >= 0 else DEFAULT_STALE_HOURS


def _head_fields(path: str) -> Optional[dict[str, str]]:
    """`status`/`to`/`title` from the leading `---` block, read from the first 4 KiB only."""
    try:
        with open(path, "rb") as fh:
            buf = fh.read(_HEAD_CAP)
    except OSError:
        return None
    lines = buf.decode("utf-8", errors="replace").split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    out: dict[str, str] = {}
    for line in lines[1:]:
        line = line.rstrip("\r")
        if line == "---":
            return out
        key, sep, value = line.partition(":")
        if sep and key in ("status", "to", "title"):
            out[key] = value.strip().strip("'\"")
    return out


def stale_outbox_drafts(repo_root: Path, now: Optional[float] = None) -> tuple[int, str]:
    """One `  - <topic> (<N>h, to: <to>) <title>` line per stale draft, then the offer line.

    Sorted by topic (the filename stem) then root; N is whole hours of mtime age; the threshold is
    strictly more than the stale window. `now` is epoch seconds, injectable for tests.
    """
    current = time.time() if now is None else now
    window = _stale_hours()
    found: list[tuple[str, int, str, str]] = []
    for rel in _ROOTS:
        directory = os.path.join(str(repo_root), *rel.split("/"))
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".md"):
                continue
            try:
                if not entry.is_file():
                    continue
                age_hours = (current - entry.stat().st_mtime) / 3600.0
            except OSError:
                continue
            if age_hours <= window:
                continue
            fields = _head_fields(entry.path)
            if not fields or fields.get("status") != "draft":
                continue
            found.append((entry.name[:-3], int(age_hours), fields.get("to", ""), fields.get("title", "")))
    if not found:
        return 0, ""
    found.sort(key=lambda r: r[0])
    lines = [f"  - {topic} ({hours}h, to: {to or '?'}) {title}".rstrip() for topic, hours, to, title in found]
    lines.append(OFFER)
    return 0, "\n".join(lines)
