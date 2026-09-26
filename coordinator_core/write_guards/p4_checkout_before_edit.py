
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from coordinator_core.p4 import runner, workspace
from coordinator_core.p4.session_change import (
    P4SessionChangeError,
    _resolve_repo_key,
    ensure_session_change,
)
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 41

_ALTERNATIVE = "Use instead:\n  report to your EM: needs a checkout this session cannot take"


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """``file_path``, falling back to ``notebook_path`` for NotebookEdit —
    same convention as the other ported write guards (INTERFACE.md)."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _resolve_abs_path(file_path: str, repo_root: str) -> str:
    if os.path.isabs(file_path):
        return file_path
    return str(Path(repo_root) / file_path)


def _is_writable(abs_path: str) -> bool:
    if not os.path.exists(abs_path):
        return True
    return os.access(abs_path, os.W_OK)


def _parse_ztag(stdout: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if not line.startswith("... "):
            continue
        rest = line[4:]
        name, _, value = rest.partition(" ")
        if not name:
            continue
        fields[name] = value
    return fields


def _lock_reason(fields: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    head_type = fields.get("headType", "")
    if "binary" in head_type:
        return "binary file", None

    other_open_raw = fields.get("otherOpen", "")
    try:
        other_open_count = int(other_open_raw) if other_open_raw else 0
    except ValueError:
        other_open_count = 1
    if "+l" in head_type and other_open_count > 0:
        return "exclusive-open (+l) held by another user", fields.get("otherOpen0") or None

    if "otherLock" in fields:
        return "locked by another user", fields.get("otherLock0") or None

    return None, None


def _deny(reason_text: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason_text,
        }
    }


def _deny_locked(abs_path: str, reason: str, holder: Optional[str]) -> Dict[str, Any]:
    holder_text = f", held by {holder}" if holder else ""
    return _deny(
        f"BLOCKED: p4 checkout unavailable for {abs_path} ({reason}{holder_text}).\n"
        f"{_ALTERNATIVE}"
    )


def _deny_refused(abs_path: str, error: "runner.P4Error") -> Dict[str, Any]:
    detail = error.raw or error.kind
    return _deny(
        f"BLOCKED: p4 checkout failed for {abs_path} ({error.kind}: {detail}).\n"
        f"{_ALTERNATIVE}"
    )


def _deny_plain(abs_path: str, detail: str) -> Dict[str, Any]:
    return _deny(f"BLOCKED: p4 checkout failed ({detail}) for {abs_path}.\n{_ALTERNATIVE}")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    repo_root = resolve_repo_root(payload.get("cwd"))
    if not repo_root:
        return None

    if not workspace.is_p4_repo(repo_root):
        return None

    abs_path = _resolve_abs_path(file_path, repo_root)

    if _is_writable(abs_path):
        return None

    try:
        repo_key = _resolve_repo_key(repo_root)
        identity = workspace.identity(repo_key)
    except (P4SessionChangeError, workspace.P4WorkspaceUnregistered) as exc:
        return _deny_plain(abs_path, f"p4 workspace unregistered: {exc}")

    fstat = runner.run(
        identity.port,
        identity.user,
        identity.client,
        ["-ztag", "fstat", "-T", "headType,otherOpen,otherLock", abs_path],
        cwd=repo_root,
        timeout=runner.DEFAULT_TIMEOUT_S,
    )
    if fstat.error is not None:
        return _deny_refused(abs_path, fstat.error)

    fields = _parse_ztag(fstat.stdout)
    reason, holder = _lock_reason(fields)
    if reason is not None:
        return _deny_locked(abs_path, reason, holder)

    session_id = payload.get("session_id") or ""
    try:
        cl = ensure_session_change(repo_root, session_id)
    except P4SessionChangeError as exc:
        return _deny_plain(abs_path, f"session changelist unavailable: {exc}")

    edit = runner.run(
        identity.port,
        identity.user,
        identity.client,
        ["edit", "-c", str(cl), abs_path],
        cwd=repo_root,
        timeout=runner.DEFAULT_TIMEOUT_S,
    )
    if edit.error is not None:
        return _deny_refused(abs_path, edit.error)

    return None
