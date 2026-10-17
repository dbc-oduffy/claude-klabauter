"""coordinator_core.write_guards._memory_reroute -- write refused memory content as a lesson.

``reroute_to_lesson`` turns the content of an over-cap memory write into one
``state/lessons/`` record through ``queue_append.append_queue_entry`` and
returns its absolute path, claiming it for the writing session so the
own-left-behind commit carry picks it up; ``added_lines`` isolates what a MEMORY.md write
added. Never raises: any failure is ``None`` and the caller keeps its plain
deny. Spawns nothing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from coordinator_core.write_guards._repo_root import resolve_repo_root

_TAG = "rerouted-from-memory"
_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def added_lines(old: str, new: str) -> str:
    """Lines of ``new`` absent from ``old``, in order, newline-joined."""
    seen = set(old.splitlines())
    return "\n".join(line for line in new.splitlines() if line not in seen)


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


def _title(content: str, filename: str) -> str:
    return (
        _frontmatter_value(content, "name")
        or _frontmatter_value(content, "description")
        or Path(filename).stem
    )


def _claim(out: object, root: Optional[str], session_id: str) -> None:
    """Claim the written lesson for ``session_id`` with a repo-relative path (zero
    spawns); skipped silently when unclaimable, never fails the reroute."""
    if not (root and session_id):
        return
    try:
        from coordinator_core.session import scope as session_scope

        rel = os.path.relpath(os.path.realpath(str(out)), os.path.realpath(root))
        if rel.startswith(".."):
            return
        session_scope.touch_written_path(session_id, Path(rel).as_posix(), str(root))
    except Exception:
        return


def reroute_to_lesson(
    content: str, *, display: str, filename: str, cwd: Optional[str], session_id: str
) -> Optional[str]:
    """Absolute path of the lesson written from ``content``, else ``None``."""
    try:
        from coordinator_core.ops.queue_append import append_queue_entry

        root = resolve_repo_root(cwd)
        result = append_queue_entry(
            "lessons",
            title=_title(content, filename),
            body=content,
            scope="project",
            status="open",
            tags=[_TAG],
            evidence=f"rerouted from {display} by guard_memory_store_cap",
            caller_worktree=Path(root) if root else None,
            session_id=session_id or "",
        )
        out = result.get("out_path")
        if not out:
            return None
        _claim(out, root, session_id)
        return str(out)
    except Exception:
        return None
