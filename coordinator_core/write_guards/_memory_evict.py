"""coordinator_core.write_guards._memory_evict -- evict the oldest memory file into a lesson.

``evict_oldest`` writes the oldest-mtime body file of a project ``memory/``
dir into one ``state/lessons/`` record (``queue_append``; the project root is
the payload ``cwd``'s repo, or ``QUEUE_APPEND_OUTPUT_ROOT`` in tests), removes
it together with every ``MEMORY.md`` index row linking it, and appends one
``{path, name, type, evicted_at, lesson}`` JSON line to
``memory-evictions.jsonl`` in the dir's parent (outside ``memory/``, so the
log counts toward no cap).
Order: lesson, unlink, then log, then drop index rows (a failed row drop is
tolerated, and runs even when the log write fails, so the index never keeps a
row for an unlinked file); a failed lesson write unlinks and logs nothing, so
no memory is lost. Raises ``OSError`` on failure; the caller degrades to a
deny. Spawns nothing.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

INDEX_NAME = "MEMORY.md"
LOG_NAME = "memory-evictions.jsonl"
LESSON_TAG = "evicted-from-memory"

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


def _claim(out: str, cwd: Optional[str], session_id: str) -> None:
    """Claim the lesson for ``session_id`` so the commit carry picks it up;
    skipped silently when unclaimable."""
    if not session_id:
        return
    try:
        from coordinator_core.session import scope as session_scope
        from coordinator_core.write_guards._repo_root import resolve_repo_root

        root = resolve_repo_root(cwd)
        if not root:
            return
        rel = os.path.relpath(os.path.realpath(out), os.path.realpath(root))
        if not rel.startswith(".."):
            session_scope.touch_written_path(session_id, Path(rel).as_posix(), str(root))
    except Exception:
        return


def _write_lesson(
    content: str, name: str, cwd: Optional[str], session_id: str
) -> str:
    """Write ``content`` as one lesson record; absolute path. Raises ``OSError``."""
    try:
        from coordinator_core.ops.queue_append import append_queue_entry
        from coordinator_core.write_guards._repo_root import resolve_repo_root

        root = resolve_repo_root(cwd)
        result = append_queue_entry(
            "lessons",
            title=_frontmatter_value(content, "name") or Path(name).stem,
            body=content,
            scope="project",
            status="open",
            tags=[LESSON_TAG],
            evidence=f"evicted from memory/{name} at the memory-store cap",
            caller_worktree=Path(root) if root else None,
            session_id=session_id or "",
        )
        out = result.get("out_path")
    except Exception as exc:
        raise OSError(f"lesson write failed: {exc}") from exc
    if not out:
        raise OSError("lesson write returned no path")
    _claim(str(out), cwd, session_id)
    return str(out)


def evict_oldest(
    mem_dir: Path, *, cwd: Optional[str] = None, session_id: str = ""
) -> Optional[dict]:
    """Evict the oldest body file of ``mem_dir`` into a lesson and drop its
    index rows; return the logged record, or ``None`` when the dir holds no
    body file. ``cwd``/``session_id`` locate the lessons root and claim."""
    files = [
        p for p in mem_dir.iterdir() if p.is_file() and p.name.casefold() != INDEX_NAME.casefold()
    ]
    if not files:
        return None
    oldest = min(files, key=lambda p: p.stat().st_mtime)
    text = oldest.read_text(encoding="utf-8", errors="replace")
    record = {
        "path": str(oldest),
        "name": oldest.name,
        "type": _frontmatter_value(text, "type") or "unknown",
        "evicted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "lesson": _write_lesson(text, oldest.name, cwd, session_id),
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
