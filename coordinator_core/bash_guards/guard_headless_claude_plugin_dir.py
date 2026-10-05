"""coordinator_core.bash_guards.guard_headless_claude_plugin_dir --
``check_headless_claude_plugin_dir``: a headless ``claude -p`` / ``--print`` /
``--bg`` launch that carries no ``--plugin-dir`` is rewritten to carry the resolved
coordinator plugin root; an unresolvable root allows with an advisory. Dev
install only: without the sentinel the guard is a no-op.

Command words are located by a quote-aware scan of the raw text (segments split
on unquoted ``;``, ``&``, ``|``, newline), so ``claude`` inside a quoted string
or as an argument never matches, and the insertion point is a byte offset in
the original command. Pure string work plus the in-process resolver -- no spawn.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import token_matches_binary
from coordinator_core.bash_guards._rewrite_support import (
    _advisory,
    _allow_rewrite,
    _bt_peel_wrapper_prefix,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES

_SEPARATORS = ";&|\n"
_Word = Tuple[int, int, str]
_HEADLESS_FLAGS = ("-p", "--print", "--bg")


def _scan_segments(cmd: str) -> List[List[_Word]]:
    """Split `cmd` into segments of raw words: ``(start, end, raw_text)``."""
    segments: List[List[_Word]] = []
    words: List[_Word] = []
    n = len(cmd)
    i = 0
    start = -1
    quote = ""

    def close(end: int) -> None:
        nonlocal start
        if start >= 0:
            words.append((start, end, cmd[start:end]))
            start = -1

    while i < n:
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < n:
                i += 1
            i += 1
            continue
        if ch in "'\"":
            if start < 0:
                start = i
            quote = ch
        elif ch == "\\" and i + 1 < n and cmd[i + 1] != "\n":
            if start < 0:
                start = i
            i += 1
        elif ch in _SEPARATORS:
            close(i)
            if words:
                segments.append(words)
                words = []
        elif ch in " \t\r":
            close(i)
        elif start < 0:
            start = i
        i += 1
    if quote:
        return []
    close(n)
    if words:
        segments.append(words)
    return segments


def _plain(raw: str) -> str:
    if "'" not in raw and '"' not in raw:
        return raw
    try:
        parts = shlex.split(raw)
    except ValueError:
        return raw
    return parts[0] if len(parts) == 1 else raw


def _launch_index(words: List[_Word]) -> Optional[int]:
    """Index of the `claude` command word in a segment, or None."""
    plain = [_plain(w[2]) for w in words]
    peeled = _bt_peel_wrapper_prefix(plain)
    idx = len(plain) - len(peeled)
    if idx < len(plain) and token_matches_binary(plain[idx], "claude"):
        return idx
    return None


def _is_dev_install() -> bool:
    """True only where a registered repo carries the `.coordinator-dev-repo` sentinel."""
    try:
        from coordinator_core._fleet_names import doctrine_repo_name

        return doctrine_repo_name() is not None
    except Exception:
        return False


def _plugin_root() -> Optional[str]:
    try:
        from coordinator_core.ops.workflow_fire.fire import _native_plugin_dir

        return _native_plugin_dir()
    except Exception:
        return None


def check_headless_claude_plugin_dir(
    cmd: str, session_id: str = "", payload: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    if not cmd or "claude" not in cmd.lower():
        return None
    inserts: List[int] = []
    for words in _scan_segments(cmd):
        idx = _launch_index(words)
        if idx is None:
            continue
        args = [_plain(w[2]) for w in words[idx + 1:]]
        if not any(a in _HEADLESS_FLAGS for a in args):
            continue
        if any(a == "--plugin-dir" or a.startswith("--plugin-dir=") for a in args):
            continue
        inserts.append(words[idx][1])
    if not inserts:
        return None
    if not _is_dev_install():
        return None
    root = _plugin_root()
    if not root:
        return _advisory(
            "Coordinator plugin not found; this headless `claude` launch runs with "
            "no coordinator skills or agents. Launch via `claude-author`."
        )
    flag = " --plugin-dir " + shlex.quote(root.replace("\\", "/"))
    new_cmd = cmd
    for pos in reversed(inserts):
        new_cmd = new_cmd[:pos] + flag + new_cmd[pos:]
    return _allow_rewrite(
        new_cmd,
        "Headless `claude` launch rewritten to add --plugin-dir %s." % root.replace("\\", "/"),
    )
