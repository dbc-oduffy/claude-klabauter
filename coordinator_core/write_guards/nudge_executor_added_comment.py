"""Advisory guard: warns an executor subagent of each untagged comment its Write/Edit/MultiEdit adds."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 109

_MAX_FINDINGS = 5
_MAX_BEFORE_BYTES = 1024 * 1024
_MAX_EXCERPT = 80


def _is_executor_type(subagent_type: str) -> bool:
    return subagent_type.rsplit(":", 1)[-1].startswith("executor")


def _executor_identity(payload: Dict[str, Any], root: str) -> bool:
    from coordinator_core.write_guards._subagent_identity import (
        _read_backpointer_subagent_type,
        _resolve_subagent_identity,
    )

    raw_agent_id = payload.get("agent_id") or ""
    if not raw_agent_id:
        return False
    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
    if not agent_id:
        return False
    subagent_type = _read_backpointer_subagent_type(
        root, agent_id, expected_em_session_id=session_id
    )
    return _is_executor_type(subagent_type)


def _relative(file_path: str, root: Path, cwd: Optional[str]) -> Optional[str]:
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _read_before(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_BEFORE_BYTES:
            raise OSError("too large")
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _excerpt(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= _MAX_EXCERPT else one[: _MAX_EXCERPT - 3] + "..."


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if (payload.get("tool_name") or "") not in MATCHERS:
            return None
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        file_path = tool_input.get("file_path") or ""
        if not file_path or not payload.get("agent_id"):
            return None

        from coordinator_core.write_guards._repo_root import resolve_repo_root

        root_str = resolve_repo_root(payload.get("cwd"))
        if not root_str:
            return None
        if not _executor_identity(payload, root_str):
            return None

        root = Path(root_str)
        rel = _relative(str(file_path), root, payload.get("cwd"))
        if rel is None:
            return None

        from coordinator_core.commenting.added import added_comments
        from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

        before = _read_before(root / rel)
        after = reconstruct_after(payload.get("tool_name") or "", tool_input, before)
        if after is None:
            return None

        found = added_comments(rel, before, after)
        if not found:
            return None

        lines = [
            f"[executor-comment] {c.path}:{c.line} added {c.kind}: {_excerpt(c.text)}"
            for c in found[:_MAX_FINDINGS]
        ]
        if len(found) > _MAX_FINDINGS:
            lines.append(f"[executor-comment] (+{len(found) - _MAX_FINDINGS} more)")
        lines.append(
            "[executor-comment] WARNING: comments are presumed unjustified; each becomes a major finding at land. "
            "Delete it: the why belongs in the plan or commit, which the Governed-By trailer traces to, "
            "and what the code does is read from the code. "
            "Keep it only if it opens with trap:, invariant: or external-constraint: "
            "and you justify the keep in your report, naming what a reader of the code alone would miss."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n".join(lines),
            }
        }
    except Exception:
        return None
