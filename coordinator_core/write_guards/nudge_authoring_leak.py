"""coordinator_core.write_guards.nudge_authoring_leak -- advisory guard.

Offers a heads-up at write time when an Edit/Write/MultiEdit would add a NEW
payload-locality or foreign-identity violation, before the commit gate
(`authoring_leaks.leak_gate`) refuses it. The commit gate stays the authority;
this guard only surfaces the finding early.

The before-text is the target's current on-disk content (nothing is read from
HEAD and no rendering happens on the before side); the after-text comes from
`_sentinel_write_guard.reconstruct_after`. Findings come from the detectors'
text-level entries (`detect_text`), capped at `_MAX_FINDINGS` context lines.

Negative-spec:
  - CLASS is "advisory": the envelope carries `additionalContext` only, never
    a permission decision.
  - Out-of-scope paths return None before any file read.
  - Zero spawns; any exception returns None (fail open).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 226

_MAX_FINDINGS = 5
_MAX_BEFORE_BYTES = 1024 * 1024


def _relative_to_root(file_path: str, payload: Dict[str, Any]) -> Optional[tuple]:
    """`(root, repo-relative forward-slash path)` or None when the target is outside the repo."""
    from coordinator_core.write_guards._repo_root import resolve_repo_root

    p = Path(file_path)
    start = payload.get("cwd") if not p.is_absolute() else str(p.parent)
    root_str = resolve_repo_root(start or None)
    if not root_str:
        return None
    root = Path(root_str)
    if not p.is_absolute():
        p = Path(start or os.getcwd()) / p
    try:
        rel = p.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return root, rel.as_posix()


def _in_scope(rel: str) -> bool:
    from coordinator_core.authoring_leaks import foreign_identity, payload_locality

    return foreign_identity.in_scope(rel) or payload_locality.target_for(rel) is not None


def _read_before(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_BEFORE_BYTES:
            raise OSError("too large")
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _findings(before: str, after: str, rel: str, root: Path) -> List[Any]:
    from coordinator_core.authoring_leaks import foreign_identity, payload_locality

    found: List[Any] = []
    found.extend(payload_locality.detect_text(before, after, rel, root))
    found.extend(foreign_identity.detect_text(before, after, rel))
    return found


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in MATCHERS:
            return None
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None
        file_path = tool_input.get("file_path") or ""
        if not file_path:
            return None

        resolved = _relative_to_root(str(file_path), payload)
        if resolved is None:
            return None
        root, rel = resolved
        if not _in_scope(rel):
            return None

        from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

        before = _read_before(root / rel)
        after = reconstruct_after(tool_name, tool_input, before)
        if after is None:
            return None

        found = _findings(before, after, rel, root)
        if not found:
            return None

        lines = [f"[authoring-leak] {f.render()}" for f in found[:_MAX_FINDINGS]]
        if len(found) > _MAX_FINDINGS:
            lines.append(f"[authoring-leak] (+{len(found) - _MAX_FINDINGS} more)")
        lines.append("[authoring-leak] advisory only; the commit gate refuses these.")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n".join(lines),
            }
        }
    except Exception:
        return None
