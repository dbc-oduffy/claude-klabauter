"""coordinator_core.write_guards.block_hand_authored_handoff_creation --
hard-deny guard.

Purpose: refuse a hand-created handoff file at the point it is CREATED. A
handoff born by Write skips the scaffold's frontmatter, lineage stamping and
chain bookkeeping that ``baton-assemble`` / ``coordinator-doc-new`` apply.

Scope:
  - MATCHERS = Write / Edit / MultiEdit: any tool that can create a file.
  - Path shape: any file under ``.claude/handoffs/`` or ``state/handoffs/``.
  - Fires ONLY when the target does not already exist (an edit to a
    scaffolded or live handoff is normal authoring and always passes).
  - The sanctioned producers (``baton-assemble``, ``coordinator-doc-new``,
    including its ``--type recovery`` scaffold for ``kind: recovery`` batons)
    write with ``open()`` inside the engine, which never reaches PreToolUse;
    the body fill that follows is an Edit of an existing path. Provenance is
    therefore structural, exactly as for the sidecar sibling.
  - Bash redirect / ``tee`` creation is the twin
    ``bash_guards.block_hand_authored_handoff_creation``; this module owns the
    path predicate and the deny text both share.

Never fails closed on an unexpected error.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 61

OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_HAND_HANDOFF_WRITE"

#: A file under either handoff home. Case-insensitive: NTFS and default APFS
#: name the same directory under any casing.
HANDOFF_PATH_RE = re.compile(
    r"(^|/)(\.claude/handoffs|state/handoffs)/[^/].*$", re.IGNORECASE
)

DENY_REASON = (
    "Handoffs are written by /handoff (baton-assemble), not by hand.\n"
    "Use instead: `coordinator-doc-new --type handoff`"
)


def normalize(file_path: str) -> str:
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def resolve_against_cwd(file_path: str, cwd: Optional[str]) -> str:
    """Resolve a possibly-relative path against the payload's cwd, not the
    guard process's own."""
    normalized = normalize(file_path)
    absolute = normalized.startswith("/") or (
        len(normalized) >= 2 and normalized[1] == ":"
    )
    if not absolute:
        normalized = normalize(str(Path(cwd or ".") / normalized))
    return posixpath.normpath(normalized)


def is_new_handoff_path(file_path: str, cwd: Optional[str]) -> bool:
    """True when ``file_path`` is under a handoff home and does not exist."""
    if HANDOFF_PATH_RE.search(normalize(file_path)) is None:
        return False
    resolved = resolve_against_cwd(file_path, cwd)
    if HANDOFF_PATH_RE.search(resolved) is None:
        return False
    return not os.path.exists(resolved)


def deny_envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    note = operator_override_note(OVERRIDE_ENV_VAR, payload=payload)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON + ("\n\n" + note if note else ""),
        }
    }


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(OVERRIDE_ENV_VAR, "0") == "1":
            return None
        if (payload.get("tool_name") or "") not in MATCHERS:
            return None
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        file_path = tool_input.get("file_path") or ""
        if not file_path:
            return None
        if not is_new_handoff_path(file_path, payload.get("cwd")):
            return None
        return deny_envelope(payload)
    except Exception:
        return None
