"""coordinator_core.write_guards.block_home_root_write -- hard-deny guard.

Purpose: refuse a Write/Edit/MultiEdit/NotebookEdit that CREATES a file directly
in the home dir. The predicate and deny text live in ``write_guards._home_root``
and are shared with the Bash/PowerShell twin. Existing files, home
subdirectories and tool-owned dotfiles pass. Never fails closed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.write_guards._home_root import DENY_REASON, creates_home_root_entry

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 133


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not path or not creates_home_root_entry(path, payload.get("cwd")):
            return None
        return apply_guard_level(
            "block-home-root-write",
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": DENY_REASON,
                }
            },
        )
    except Exception:
        return None
