"""coordinator_core.write_guards.block_em_strict_dispatch_code_write --
OPT-IN hard-deny: with `COORDINATOR_STRICT_DISPATCH` set (1/true/on) in the
session's environment, the MAIN EM session may not Write/Edit/MultiEdit a
code file until it has sized the work this session, and not at all once the
newest sizing is `S` or larger. `XS` stays EM-typed.

Built for a cloud experiment; inert everywhere else. The flag is read by
`engine._ENV_GATED_GUARDS`, not here: flag unset, hot-path discovery skips
this module before reading or importing it, so an unflagged write pays one
dict lookup. The flag is an environment variable only, never a fleet mode
key: a fleet record would cost a file read on every write in every session.

Deny conditions, once flagged and the caller is the main EM session:
  1. No sizing object (`state/sizings/*.yaml|yml`) touched this session ->
     run `coordinator:sizing` first.
  2. The newest touched sizing object (by mtime; the touch record keeps
     first-touch order, so its tail is not the newest) has `estimate.tshirt`
     S..XXL, or none parseable -> dispatch executors instead.

Subagents always pass: `agent_id` in the payload, or the house
`em-session-id.txt` back-pointer test (`_is_subagent_session`). Ambiguity
allows. Doc/data files always pass (`_DOC_DATA_EXTENSIONS`, imported).

Known gap: code written through Bash (heredoc, `cp`) is not a matched tool
and passes.

Any unexpected exception allows.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.hooks.nudge_em_code_dispatch import _DOC_DATA_EXTENSIONS
from coordinator_core.hooks.nudge_unrouted_sizing import (
    _is_subagent_session,
    _session_touched_sizing_files,
)
from coordinator_core.ops.read_sizing_object_fields import _read_sizing_object_fields
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 138

_CONTROL_WHITESPACE_RE = re.compile(r"[\t\r\n\f\v]")
_C0_CONTROL_RE = re.compile(r"[\x00-\x1f]")

_DENY_TSHIRTS = frozenset({"S", "M", "L", "XL", "XXL"})


def _sanitize_for_reason(s: str) -> str:
    safe = _CONTROL_WHITESPACE_RE.sub(" ", s)
    return _C0_CONTROL_RE.sub("", safe)


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    val = tool_input.get("file_path")
    return val if isinstance(val, str) and val else ""


def _ext_of(file_path: str) -> str:
    dot = file_path.rfind(".")
    slash = max(file_path.rfind("/"), file_path.rfind("\\"))
    if dot <= slash:
        return ""
    return file_path[dot:].lower()


def _no_sizing_deny() -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "strict_dispatch: no sizing this session.\n"
                "Use instead: run coordinator:sizing first."
            ),
        }
    }


def _dispatch_deny(tshirt_display: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"strict_dispatch: sized {tshirt_display} — EM does not type "
                "code at ≥S.\n"
                "Use instead: dispatch an executor (plan -> execute-plan / "
                "Workflow)."
            ),
        }
    }


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if (payload.get("tool_name") or "") not in MATCHERS:
            return None

        session_id = payload.get("session_id") or ""

        if payload.get("agent_id"):
            return None

        repo_root = resolve_repo_root(payload.get("cwd"))
        if not repo_root:
            return None
        if _is_subagent_session(session_id, repo_root):
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None
        if _ext_of(file_path) in _DOC_DATA_EXTENSIONS:
            return None

        touched = _session_touched_sizing_files(session_id, repo_root)
        paths = [os.path.join(repo_root, t) for t in touched]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return _no_sizing_deny()
        fields = _read_sizing_object_fields(max(paths, key=os.path.getmtime))
        estimate = fields.get("estimate")
        tshirt = estimate.get("tshirt") if isinstance(estimate, dict) else None

        if not isinstance(tshirt, str) or not tshirt.strip():
            return _dispatch_deny("<missing/unparseable tshirt, treated as >= S>")

        tshirt_norm = tshirt.strip().upper()
        if tshirt_norm == "XS":
            return None
        if tshirt_norm in _DENY_TSHIRTS:
            return _dispatch_deny(tshirt_norm)
        return _dispatch_deny(
            f"<unparseable tshirt {_sanitize_for_reason(tshirt_norm)!r}, treated as >= S>"
        )
    except Exception:
        return None
