"""Shared helper: the body a file WILL have after a Write/Edit/MultiEdit call.

Advisory write-guards that judge the resulting text (not the fragment the
tool call carries) all need the same three steps: read the on-disk pre-image
under a size cap, apply the call's substitutions to it, and fail open to
``None`` when the result cannot be constructed. This module is the one
implementation of those steps.

The underscore prefix is load-bearing: `engine.py::_discover_guards()` skips
modules whose name starts with `_`, so this stays shared plumbing rather than
a registered guard of its own.

Contract:
  - ``Write`` returns its ``content`` outright (``None`` when not a string).
  - ``Edit`` / ``MultiEdit`` substitute into the pre-image; ``replace_all``
    truthy replaces every occurrence, otherwise only the first.
  - ``None`` always means "cannot construct the body" — callers go silent,
    never fall back to a fragment-scoped judgment.
  - A ``MultiEdit`` fragment that is malformed or whose ``old_string`` is
    absent from the running body (stale) aborts the whole reconstruction by
    default, matching the tool's all-or-nothing semantics. ``skip_stale=True``
    drops such a fragment and carries on with the rest.
  - Never raises on an unexpected payload shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Union

MAX_WHOLE_FILE_BYTES = 256 * 1024


def read_pre_image(path: Union[str, Path]) -> Optional[str]:
    """On-disk text of ``path``, or ``None`` when missing, unreadable, or over
    ``MAX_WHOLE_FILE_BYTES``."""
    try:
        target = Path(path)
        if target.stat().st_size > MAX_WHOLE_FILE_BYTES:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def apply_edit(
    content: str, old_string: Any, new_string: Any, replace_all: Any
) -> Optional[str]:
    """One old->new substitution, or ``None`` when ``old_string`` is not a
    non-empty string present in ``content`` or ``new_string`` is not a string."""
    if not isinstance(old_string, str) or not old_string:
        return None
    if not isinstance(new_string, str):
        return None
    if old_string not in content:
        return None
    if replace_all:
        return content.replace(old_string, new_string)
    return content.replace(old_string, new_string, 1)


def post_write_body(
    tool_name: str,
    tool_input: Dict[str, Any],
    pre_image: Optional[str],
    *,
    skip_stale: bool = False,
) -> Optional[str]:
    """The full text the file will hold after this tool call, or ``None``."""
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    if pre_image is None:
        return None

    if tool_name == "Edit":
        return apply_edit(
            pre_image,
            tool_input.get("old_string"),
            tool_input.get("new_string"),
            tool_input.get("replace_all"),
        )

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        body = pre_image
        for edit in edits:
            result = (
                apply_edit(
                    body,
                    edit.get("old_string"),
                    edit.get("new_string"),
                    edit.get("replace_all"),
                )
                if isinstance(edit, dict)
                else None
            )
            if result is None:
                if skip_stale:
                    continue
                return None
            body = result
        return body

    return None
