"""coordinator_core.write_guards._memory_evict -- evict the oldest memory file.

``evict_oldest`` removes the oldest-mtime body file of a project ``memory/``
dir together with every ``MEMORY.md`` index row linking it, and appends one
``{path, name, type, evicted_at}`` JSON line to ``memory-evictions.jsonl`` in
the dir's parent (outside ``memory/``, so the log counts toward no cap).
Order: unlink, then log, then drop index rows (a failed row drop is
tolerated, and runs even when the log write fails, so the index never keeps a
row for an unlinked file); a failed unlink logs nothing. Raises ``OSError`` on failure; the
caller degrades to a deny. Spawns nothing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

INDEX_NAME = "MEMORY.md"
LOG_NAME = "memory-evictions.jsonl"

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_LINK_TARGET_RE = re.compile(r"\]\(([^)\s]+)\)")


def _frontmatter_value(content: str, key: str) -> Optional[str]:
    m = _FM_RE.match(content)
    if not m:
        return None
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() == key:
            v = v.strip().strip("'\"").strip()
            return v or None
    return None


def _row_links(row: str, name: str) -> bool:
    return any(t == name for t in _LINK_TARGET_RE.findall(row))


def _drop_rows(index: Path, name: str) -> None:
    """Rewrite ``index`` without the ``"- "`` rows whose link target is ``name``."""
    try:
        text = index.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    lines = text.splitlines(keepends=True)
    kept = [ln for ln in lines if not (ln.startswith("- ") and _row_links(ln, name))]
    if len(kept) != len(lines):
        index.write_text("".join(kept), encoding="utf-8")


def evict_oldest(mem_dir: Path) -> Optional[dict]:
    """Evict the oldest body file of ``mem_dir`` and its index rows; return the
    logged record, or ``None`` when the dir holds no body file."""
    files = [
        p for p in mem_dir.iterdir() if p.is_file() and p.name.casefold() != INDEX_NAME.casefold()
    ]
    if not files:
        return None
    oldest = min(files, key=lambda p: p.stat().st_mtime)
    try:
        mem_type = _frontmatter_value(oldest.read_text(encoding="utf-8"), "type")
    except (OSError, UnicodeDecodeError):
        mem_type = None
    record = {
        "path": str(oldest),
        "name": oldest.name,
        "type": mem_type or "unknown",
        "evicted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    oldest.unlink()
    try:
        with open(mem_dir.parent / LOG_NAME, "a", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")
    finally:
        try:
            _drop_rows(mem_dir / INDEX_NAME, oldest.name)
        except (OSError, UnicodeDecodeError):
            pass
    return record
