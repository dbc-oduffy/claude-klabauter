"""Advisory for a foreground Bash call whose command ends in a job-control `&`.

The backgrounded job outlives the call and reports to nobody; the harness's
`run_in_background` is the supervised form. Pure string work: no spawn, no
file read.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._rewrite_support import (
    _advisory,
    _crlf_strip,
    _override,
)

OVERRIDE_ENV = "COORDINATOR_ALLOW_TRAILING_AMPERSAND"

_HEREDOC_INTRO_RE = re.compile(
    r"<<(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))"
)

_MESSAGE = (
    "Advisory: the trailing `&` leaves a job that outlives this call and "
    "reports to nobody. Use `run_in_background: true` instead."
)


def _run_in_background(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    tool_input = payload.get("tool_input")
    for src in (tool_input, payload):
        if isinstance(src, dict) and src.get("run_in_background") is True:
            return True
    return False


def _ends_in_job_ampersand(cmd: str) -> bool:
    """True when the last top-level token is an unquoted `&` that is neither
    half of `&&`, nor `>&` / `<&`, nor a `2>&1`-style duplication.

    Heredoc bodies are skipped line-wise; a trailing `&` is judged on the
    command text outside them.
    """
    lines = cmd.split("\n")
    kept = []
    hd_word: Optional[str] = None
    hd_strip = False
    for line in lines:
        if hd_word is not None:
            check = line.lstrip("\t") if hd_strip else line
            if check == hd_word:
                hd_word = None
            continue
        kept.append(line)
        m = _HEREDOC_INTRO_RE.search(line)
        if m and "<<<" not in line:
            hd_strip = m.group(1) == "-"
            hd_word = m.group(2) or m.group(3) or m.group(4)
    text = "\n".join(kept).rstrip()
    if not text.endswith("&"):
        return False

    in_single = in_double = False
    i = 0
    n = len(text)
    last_amp_unquoted = -1
    while i < n:
        c = text[i]
        if c == "\\" and not in_single:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "&" and not in_single and not in_double:
            last_amp_unquoted = i
        i += 1
    if last_amp_unquoted != n - 1 or in_single or in_double:
        return False
    if n >= 2 and text[n - 2] in "&>|<":
        return False
    return True


def check_trailing_background_ampersand(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Advisory envelope when a foreground call ends in a job `&`, else None."""
    if not cmd or "&" not in cmd:
        return None
    if _override(OVERRIDE_ENV, payload=payload):
        return None
    if _run_in_background(payload):
        return None
    if not _ends_in_job_ampersand(_crlf_strip(cmd)):
        return None
    return _advisory(_MESSAGE)
