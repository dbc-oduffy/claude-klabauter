"""coordinator_core.bash_guards.block_venv_creation -- PreToolUse(Bash)
advisory guard for the fleet-wide per-repo/shared-venv ban (PM directive,
2026-09-29: system interpreter only, no per-repo or shared venvs; the
engine's own `fleet_env`/`ensure_venv` provisioners are being retired in
the same chunk this guard ships in).

POSTURE -- ADVISORY, NOT DENY (PM amendment mid-chunk, 2026-09-29,
superseding the chunk's original "hard deny, no override key" spec). This
guard WARNS and lets the command run: `check()` always returns an
``allow`` verdict, carrying ``additionalContext`` naming the tripwire
(``PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE``) when a venv-creation shape is
recognized, and ``None`` (silent allow) otherwise. There is deliberately no
override key here -- an advisory that never blocks needs nothing to bypass.

Detection reuses the same tokenizer helpers `block_worktree_creation.py`
already reuses from `block_subagent_destructive_action.py` (heredoc-body
stripping, segment splitting on `&&`/`;`/`|`, leading env-assignment/
wrapper skipping, basename normalization) rather than a raw substring scan
-- so a document-persisting heredoc that merely NAMES `python -m venv` in
its prose does not fire, matching the sibling guard's own false-deny fix.

Recognized shapes (the executable resolves via `_normalize_executable_
basename`, so a full path or `.exe` suffix still matches):
  - `python`/`python3`/`py` with `-m venv` anywhere in its argv
  - `virtualenv ...`
  - `uv venv` / `uv sync`
  - `pipenv install|sync|shell`
  - `poetry install`
  - `conda create`

Deliberately NOT matched: `grep venv`, `ls .venv`, or any other command
whose argv merely contains the substring `venv` without the executable
itself being one of the above -- this guard classifies by (executable,
subcommand) token pairs, never by regexing the raw command text for the
word `venv`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

_PY_EXECUTABLES = frozenset({"python", "python3", "py"})
_TRIPWIRE = "PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE"


def _classify_segment(working: "list[str]") -> Optional[str]:
    if not working:
        return None
    head = _normalize_executable_basename(working[0])
    rest = working[1:]

    if head in _PY_EXECUTABLES:
        for i, tok in enumerate(rest):
            if tok == "-m" and i + 1 < len(rest) and rest[i + 1] == "venv":
                return "%s -m venv" % head
        return None

    if head == "virtualenv":
        return "virtualenv"

    if head == "uv":
        if rest and rest[0] in ("venv", "sync"):
            return "uv %s" % rest[0]
        return None

    if head == "pipenv":
        if rest and rest[0] in ("install", "sync", "shell"):
            return "pipenv %s" % rest[0]
        return None

    if head == "poetry":
        if rest and rest[0] == "install":
            return "poetry install"
        return None

    if head == "conda":
        if rest and rest[0] == "create":
            return "conda create"
        return None

    return None


def _evaluate(cmd: str) -> Optional[str]:
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return None

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        working = _strip_leading_subshell_and_env(seg_tokens)
        if not working:
            continue
        hit = _classify_segment(working)
        if hit is not None:
            return hit

    return None


def _advisory_reason(shape: str) -> str:
    return (
        "ADVISORY [%s]: `%s` creates a per-repo/shared venv -- banned "
        "fleet-wide (system interpreter only). Command still ran; no "
        "action required unless you meant to provision one."
    ) % (_TRIPWIRE, shape)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    cmd_for_classification = _strip_heredoc_bodies(cmd)
    shape = _evaluate(cmd_for_classification)
    if shape is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": _advisory_reason(shape),
        }
    }
