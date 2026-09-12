"""
coordinator_core.write_guards.p4_checkout_before_edit — checkout-before-edit
write guard (D5).

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
§ C5, § D3, § D5, § D9.

Marker check first, then a local stat, before any p4 spawn (D1: "detection is
a declared marker, never a probe"). A writable target, a nonexistent target
(a brand-new file has nothing to check out), or a git-only repo all allow
with zero spawns. A read-only target in a marker repo runs
``p4 -ztag fstat -T headType,otherOpen,otherLock`` (spawn 1), classifies the
result, then either denies or runs ``p4 edit -c <CL>`` (spawn 2) to open the
file into the session changelist — ≤2 p4 spawns total for this guard's own
budget (D5). ``ensure_session_change``'s own mint spawn, when it fires, is
C2's budget, not this row's.

Deny conditions, in order:
  1. The fstat spawn classifies to ``ticket_expired`` / ``refused(raw)``
     (a runner timeout also folds into ``refused`` — see ``p4.runner``) —
     deny with the one-line alternative, never allow-through (D5).
  2. ``headType`` names a binary type — deny, naming no holder (binary is a
     type property, not a lock).
  3. ``headType`` carries ``+l`` (exclusive-open) AND ``otherOpen`` is
     nonzero — deny, naming the holder off ``otherOpen0``.
  4. ``otherLock`` is present — deny, naming the holder off ``otherLock0``.
  5. The edit spawn itself classifies to ``ticket_expired`` / ``refused(raw)``
     — deny with the one-line alternative, never allow-through (D5). The
     checkout attempt already ran by this point; an operator unlock waives
     the deny envelope this call returns, never the open attempt already
     made (D5).

Every deny points the agent at the UE editor or example-game-repo's checkout tool
directly — no provider registry (D9).

Deny, never ask, under bypassPermissions (D5). Covers Edit, Write, MultiEdit
and NotebookEdit.

Negative-spec:
  - Never probes p4 for a writable target or a git-only repo — zero spawns.
  - Never trusts a p4 exit code — classification is ``p4.runner.run``'s
    contract, not this module's.
  - Never allows through a classified ``ticket_expired`` / ``refused`` /
    timeout outcome, from either the fstat or the edit spawn.
  - Never runs an unscoped ``fstat``/``edit`` — every spawn here is scoped to
    the one target path.
"""

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
PRIORITY = 40

_ALTERNATIVE = "Use the UE editor's checkout, or example-game-repo's checkout tool, directly."


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
    """A nonexistent target (a brand-new file) has nothing to check out and
    is treated as writable — the same zero-spawn allow as an already-open
    file."""
    if not os.path.exists(abs_path):
        return True
    return os.access(abs_path, os.W_OK)


def _parse_ztag(stdout: str) -> Dict[str, str]:
    """``p4 -ztag`` line shape: ``... <field> <value>`` (value omitted for a
    bare flag field like a lock-held ``otherLock``)."""
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
    """``(reason, holder)`` from parsed fstat fields, or ``(None, None)`` when
    none of the three deny conditions (binary / +l&otherOpen / otherLock)
    holds."""
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
    return _deny(f"BLOCKED: p4 checkout failed for {abs_path} ({detail}).\n{_ALTERNATIVE}")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the checkout-before-edit guard against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Fails open on
    a missing file_path/repo_root — matching this family's convention that
    an unresolvable write target or repo context is not this guard's call.
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    repo_root = resolve_repo_root(payload.get("cwd"))
    if not repo_root:
        return None

    # D1 marker check first — zero spawns either way.
    if not workspace.is_p4_repo(repo_root):
        return None

    abs_path = _resolve_abs_path(file_path, repo_root)

    # Local stat before any p4 spawn — writable/nonexistent target allows
    # with zero spawns.
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
    )
    if edit.error is not None:
        return _deny_refused(abs_path, edit.error)

    return None
