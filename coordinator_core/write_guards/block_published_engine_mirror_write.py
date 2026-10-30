"""coordinator_core.write_guards.block_published_engine_mirror_write -- advisory guard.

The published engine mirror (``engine_root.published_engine_mirror_path()``) is what the warm
engine runs from: an uncommitted edit there goes live box-wide and is lost on the next publish.
A Write/Edit/MultiEdit/NotebookEdit aimed at a path under it fires an advisory naming the same
relative path in the source repo (``engine_root.engine_source_root()``) as the place to edit.

Negative-spec:
  - Never denies. A write into the mirror is recoverable; CLASS is "advisory" and ``check()``
    returns only ``None`` or an ``allow_advisory`` envelope.
  - Silent when no mirror is registered, when the path is outside it, or on any internal error.
  - Does not cover Bash-tool writes (sed/heredoc into the mirror).
  - When no source root is registered the advisory names the relative path and "the source
    repo checkout" instead of an absolute path; it never guesses a root.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import allow_advisory
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._slash_normalize import collapse_slashes

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 227  # advisory band; next free slot after nudge_authoring_leak (226)


def _target_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    key = "notebook_path" if payload.get("tool_name") == "NotebookEdit" else "file_path"
    value = tool_input.get(key) or ""
    return value if isinstance(value, str) else ""


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        file_path = _target_path(payload)
        if not file_path:
            return None

        from coordinator_core.engine_root import engine_source_root, published_engine_mirror_path

        mirror = published_engine_mirror_path()
        if not mirror:
            return None

        mirror_norm = collapse_slashes(mirror).rstrip("/")
        path_norm = collapse_slashes(file_path)
        if not mirror_norm or not casefold_path(path_norm).startswith(casefold_path(mirror_norm) + "/"):
            return None

        relative = path_norm[len(mirror_norm) + 1:]
        source = engine_source_root()
        if source:
            target = collapse_slashes(source).rstrip("/") + "/" + relative
        else:
            target = f"{relative} in the source repo checkout"

        return allow_advisory(
            "PreToolUse",
            f"{file_path} is in the published engine mirror (live box-wide, overwritten on publish). "
            f"Edit the source repo instead:\n  {target}",
        )
    except Exception:
        return None
