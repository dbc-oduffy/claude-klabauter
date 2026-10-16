"""coordinator_core.write_guards.block_drive_root_new_dir_write -- hard-deny guard.

Purpose: refuse a Write/Edit/MultiEdit/NotebookEdit whose target would add a NEW
top-level entry directly under a drive root or a home root. The predicate, override
reader and deny text live in ``write_guards._drive_root``; a target directly in the
home dir stays with ``block_home_root_write``. Best-effort speedbump (DR-258): the
Stop-time drive-root leg remains the backstop. Never fails closed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.write_guards._drive_root import deny_reason, new_top_level_entry, override_active

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 134


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not path or override_active(payload):
            return None
        env = payload.get("env")
        name = new_top_level_entry(
            path,
            payload.get("cwd"),
            bare_slash_is_drive_root=True,
            env=env if isinstance(env, dict) else None,
        )
        if not name:
            return None
        return apply_guard_level(
            "block-drive-root-new-dir-write",
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": deny_reason(name),
                }
            },
        )
    except Exception:
        return None
