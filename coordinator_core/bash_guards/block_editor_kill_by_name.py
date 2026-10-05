"""coordinator_core.bash_guards.block_editor_kill_by_name -- PreToolUse(Bash,
PowerShell) hard deny, every session: no name-based kill of Unreal editor
processes.

A kill by image name takes down every session's editor on the box; two
headless runs died mid-run that way. Denied, for UnrealEditor*, UE5Editor*,
ShaderCompileWorker*, LiveCodingConsole* and UnrealTraceServer*:
  - `taskkill ... /IM <name>`;
  - `Stop-Process -Name <name>` (and its `spps`/`kill` aliases), and
    `Get-Process <name> | Stop-Process`;
  - `pkill <name>` / `killall <name>`.
PID-targeted kills (`taskkill /PID`, `Stop-Process -Id`, `kill <pid>`) are
allowed. A launched `.sh`/`.ps1`/`.py`/`.cmd`/`.bat` script that exists on
disk is read and held to the same check, so a name-kill loop cannot hide in
a watchdog script.

No override.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

_EDITOR_NAME_RE = re.compile(
    r"(?i)^[\"']?(?:UnrealEditor|UE5Editor|ShaderCompileWorker|LiveCodingConsole|UnrealTraceServer)"
)
_PRE_FILTER_RE = re.compile(
    r"(?i)UnrealEditor|UE5Editor|ShaderCompileWorker|LiveCodingConsole|UnrealTraceServer"
)
_STOP_PROCESS_NAMES = ("Stop-Process", "spps", "kill")
_GET_PROCESS_PIPE_RE = re.compile(
    r"(?i)\b(?:Get-Process|gps|ps)\b[^|;]*?(?:UnrealEditor|UE5Editor|ShaderCompileWorker|"
    r"LiveCodingConsole|UnrealTraceServer)[^|;]*\|\s*(?:Stop-Process|spps|kill)\b"
)


def _names(values: List[str]) -> List[str]:
    out = []
    for value in values:
        out.extend(v for v in value.split(",") if v)
    return [v for v in out if _EDITOR_NAME_RE.match(v)]


def _flag_values(tokens: List[str], flags: tuple) -> List[str]:
    values = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        for flag in flags:
            if low == flag and i + 1 < len(tokens):
                values.append(tokens[i + 1])
            elif low.startswith(flag + ":"):
                values.append(tok[len(flag) + 1:])
    return values


def _offence(tokens: List[str]) -> Optional[str]:
    if not tokens:
        return None
    head, args = tokens[0], tokens[1:]
    if token_matches_binary(head, "taskkill"):
        hit = _names(_flag_values(args, ("/im", "-im", "//im")))
        return f"taskkill /IM {hit[0]}" if hit else None
    if any(head.lower() == n.lower() for n in _STOP_PROCESS_NAMES):
        named = _flag_values(args, ("-name", "-processname"))
        if not named and args and not args[0].startswith("-") and not args[0].isdigit():
            named = [args[0]]
        hit = _names(named)
        return f"{head} -Name {hit[0]}" if hit else None
    if token_matches_binary(head, "pkill") or token_matches_binary(head, "killall"):
        hit = _names([a for a in args if not a.startswith("-")])
        return f"{head} {hit[0]}" if hit else None
    return None


_SCRIPT_SUFFIXES = (".sh", ".ps1", ".py", ".cmd", ".bat")
_SCRIPT_READ_CAP_BYTES = 256 * 1024


def _text_offence(text: str) -> Optional[str]:
    if not _PRE_FILTER_RE.search(text):
        return None
    text = text.replace("\r", "")
    for resolved in resolve_command_positions(text):
        found = _offence(resolved.tokens)
        if found:
            return found
    if _GET_PROCESS_PIPE_RE.search(text):
        return "Get-Process <editor> | Stop-Process"
    return None


def _script_offence(cmd: str, cwd: Optional[str]) -> Optional[str]:
    """A name-kill inside a launched script file. Unreadable files allow."""
    for resolved in resolve_command_positions(cmd):
        for tok in resolved.tokens:
            tok = tok.strip("\"'")
            if not tok.lower().endswith(_SCRIPT_SUFFIXES):
                continue
            path = tok if os.path.isabs(tok) or not cwd else os.path.join(cwd, tok)
            try:
                with open(path, "rb") as fh:
                    body = fh.read(_SCRIPT_READ_CAP_BYTES).decode("utf-8", "replace")
            except OSError:
                continue
            found = _text_offence(body)
            if found:
                return f"{tok} runs {found}"
    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    offence = _text_offence(cmd)
    if offence is None and any(sfx in cmd.lower() for sfx in _SCRIPT_SUFFIXES):
        offence = _script_offence(cmd.replace("\r", ""), payload.get("cwd"))
    if offence is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"BLOCKED: {offence}. Kill by the PID of your own launch; a name-based "
                "kill takes down other sessions' editors."
            ),
        }
    }
