
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 65

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_GOALS_LOG_WRITE"

_GOALS_LOG_RE = re.compile(r"(^|/)goals-log\.[^/]+\.jsonl$")


def _extract_file_path(tool_name: str, tool_input: Dict[str, Any]) -> str:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path") or ""
    return tool_input.get("file_path") or ""


def _normalize(file_path: str) -> str:
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = _extract_file_path(tool_name, tool_input)
        if not file_path:
            return None

        file_path_norm = _normalize(file_path)

        if not _GOALS_LOG_RE.search(file_path_norm):
            return None

        _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = (
            "Use instead:\n"
            f"  {file_path} is append-only disk-truth; a hand-write skips the "
            "goal_id content hash and status/path validation, corrupting "
            "supersession -- run append-goal-event.py (goal.append) instead."
            + ("\n\n" + _note if _note else "")
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        return None
