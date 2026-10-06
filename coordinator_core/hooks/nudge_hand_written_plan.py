"""
coordinator_core.hooks.nudge_hand_written_plan — PostToolUse advisory leg.

Purpose: when a Write lands a `docs/plans/<name>.md` whose content has no plan producer
provenance (no frontmatter, or no `plan_id: pln-...`), return one line naming the
`artifact.adopt` command that ports it to canonical shape. Advisory only; never blocks.

Fan-in: `postuse_advisory_dispatch` gathers `advisory_text` beside the other legs. A repeat
for the same (session, path) is silent via a per-pair sentinel file under the temp dir; with no
session id the leg fires every time. A Write cannot be told from an overwrite here (the stub
forwards no `tool_response`), so both fire.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile

from coordinator_core.ops.artifact_adopt_contract import (
    adopt_command,
    has_plan_producer_provenance,
    is_plan_path,
)

_PLANS_SEGMENT = "docs/plans/"


def _repo_relative(file_path: str, repo_root) -> str:
    """Repo-relative forward-slash path. Falls back to the tail from the last `docs/plans/`
    segment when `repo_root` is absent or does not contain the path."""
    norm = file_path.replace("\\", "/")
    if repo_root is not None:
        root = str(repo_root).replace("\\", "/").rstrip("/") + "/"
        if norm.lower().startswith(root.lower()):
            return norm[len(root):]
    idx = norm.rfind("/" + _PLANS_SEGMENT)
    if idx >= 0:
        return norm[idx + 1:]
    return norm


def _frontmatter_text(content: str) -> str | None:
    """The text between the opening and closing `---` lines, or None without frontmatter."""
    lines = content.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _first_time_sync(session_id: str, rel_path: str) -> bool:
    """True when this (session, path) has not been nudged; records it. Fail-open: any I/O
    error answers True."""
    if not session_id:
        return True
    # session_id is hook-payload text: hash it too so no separator or `..` reaches the path.
    sid = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:16]
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]
    sentinel = os.path.join(tempfile.gettempdir(), f"hand-written-plan-nudge-{sid}-{digest}")
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True
    os.close(fd)
    return True


async def advisory_text(tool_name: str, file_path: str, content: str, session_id: str, repo_root) -> str:
    """One-line adopt nudge for a hand-written plan Write; "" when it does not fire."""
    if tool_name != "Write" or not file_path:
        return ""
    rel = _repo_relative(file_path, repo_root)
    if not is_plan_path(rel):
        return ""
    fm = _frontmatter_text(content or "")
    if fm is not None and has_plan_producer_provenance(fm):
        return ""
    if not await asyncio.to_thread(_first_time_sync, session_id, rel):
        return ""
    reason = "no frontmatter" if fm is None else "no plan_id"
    return f"[adopt] {rel}: {reason} (hand-written). Port: {adopt_command(rel)}"
