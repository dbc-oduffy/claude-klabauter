"""coordinator_core.bash_guards.guard_background_publish -- runs a publish
round (`percolate-round`, `percolate-push`, `coordinator-publish`,
`publish.py`) as a background task, prompt-free.

A round takes minutes and is not governed by the process budget (CLAUDE.md
§ brightline: "run them in the background"); in the foreground it holds the
session hostage and hits the harness's foreground timeout mid-round, which
kills the shell but not the round or its destination lock.

Matches on the CLI name alone, so it is dialect-independent (Bash and
PowerShell alike). Rewrites `run_in_background` only; the command text is
untouched. Every other input field is carried over, because `updatedInput`
replaces the tool input wholesale.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

#: The CLI must be what a segment EXECUTES -- first word, or the script handed
#: to an interpreter -- never an argument (`grep -n x percolate-round.py`).
_CLI = r"(?:\S*[/\\])?(?:(?:percolate-round|percolate-push|coordinator-publish)(?:\.py|\.cmd|\.ps1)?|publish\.py)"
_PUBLISH_CLI_RE = re.compile(
    r"(?:^|[;&|(]|&&|\|\|)\s*(?:timeout\s+\S+\s+)?(?:[\"']?\S*(?:python3?|py|\$_py|\$\{?_py\}?)[\"']?\s+(?:-\S+\s+)*)?"
    r"[\"']?" + _CLI + r"[\"']?(?=$|[\s;&|)])",
    re.MULTILINE,
)

#: A heredoc body is data, not a command line.
_HEREDOC_BODY_RE = re.compile(
    r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?^\s*\1\s*$", re.MULTILINE | re.DOTALL
)

_NOTE = "Publish round moved to a background task; a notification arrives when it exits."


def check_background_publish(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if payload.get("tool_name") not in COMMAND_TOOL_NAMES:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("run_in_background"):
        return None
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not _PUBLISH_CLI_RE.search(_HEREDOC_BODY_RE.sub("", cmd)):
        return None
    updated = dict(tool_input)
    updated["run_in_background"] = True
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
            "additionalContext": _NOTE,
        }
    }
