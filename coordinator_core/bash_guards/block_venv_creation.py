"""coordinator_core.bash_guards.block_venv_creation -- PreToolUse(Bash |
PowerShell) guard for the fleet-wide per-repo/shared-venv ban
(``PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE``: system interpreter only).

POSTURE -- DENY for the four creation shapes, ADVISORY for the rest.
  - deny:     `python`/`python3`/`py` (also `pythonX.Y`) with `-m venv`,
              `virtualenv`, `uv venv`, `conda create`.
  - advisory: `uv sync`, `pipenv install|sync|shell`, `poetry install` --
              allow verdict carrying ``additionalContext``; not promoted to
              deny.
  - never matched: `pip --target`, `grep venv`, `ls .venv`, or any command
    whose argv merely contains the word `venv`. Classification is by
    (executable, subcommand) token pairs, never a raw-text scan.

OVERRIDE -- ``COORDINATOR_OVERRIDE_VENV_CREATION=1`` in the hook
environment, the same pre-launch key shape the sibling deny guards use. It
clears the deny and the advisory alike.

Tokenization (heredoc-body stripping, segment splitting, env-assignment and
passthrough-wrapper skipping, `sh -c` unwrapping, the PowerShell segment
resolver) is reused from `block_worktree_creation.py` and
`block_subagent_destructive_action.py`, never re-implemented. A PowerShell
command the resolver cannot parse falls back to the Bash classifier over
the text with quoted spans and here-strings stripped.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional, Tuple

from coordinator_core._hook_envelope import _stamp
from coordinator_core.bash_guards._dialect import (
    Dialect,
    _strip_ps_quotes,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
    strip_powershell_prose_noise,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _BUNDLED_C_FLAG_RE,
    _normalize_executable_basename,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards.block_worktree_creation import (
    _C_FLAG_SHELL_INTERPRETERS,
    _skip_leading_env_assignments,
)

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_VENV_CREATION"
_TRIPWIRE = "PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE"

_PY_EXE_RE = re.compile(r"^(?:python|py)[0-9.]*$")

#: (shape label, is_deny)
_Hit = Tuple[str, bool]


def _classify_segment(working: "list[str]") -> Optional[_Hit]:
    if not working:
        return None
    head = _normalize_executable_basename(working[0])
    rest = working[1:]

    if _PY_EXE_RE.match(head):
        for i, tok in enumerate(rest):
            if tok == "-m" and i + 1 < len(rest) and rest[i + 1] == "venv":
                return ("%s -m venv" % head, True)
        return None

    if head == "virtualenv":
        return ("virtualenv", True)

    if head == "uv" and rest:
        if rest[0] == "venv":
            return ("uv venv", True)
        if rest[0] == "sync":
            return ("uv sync", False)
        return None

    if head == "conda" and rest and rest[0] == "create":
        return ("conda create", True)

    if head == "pipenv" and rest and rest[0] in ("install", "sync", "shell"):
        return ("pipenv %s" % rest[0], False)

    if head == "poetry" and rest and rest[0] == "install":
        return ("poetry install", False)

    return None


def _worse(a: Optional[_Hit], b: Optional[_Hit]) -> Optional[_Hit]:
    """The deny hit wins over an advisory hit; first of equals wins."""
    if a is None:
        return b
    if b is not None and b[1] and not a[1]:
        return b
    return a


def _unwrap_shell_c(working: "list[str]") -> Optional[str]:
    """The `-c` payload of `sh|bash|zsh|dash|ksh [-ic] '<payload>'`, else None."""
    if _normalize_executable_basename(working[0]) not in _C_FLAG_SHELL_INTERPRETERS:
        return None
    for i in range(1, len(working) - 1):
        if _BUNDLED_C_FLAG_RE.match(working[i]):
            return working[i + 1]
    return None


def _scan_segments(segments, normalize) -> Optional[_Hit]:
    found: Optional[_Hit] = None
    for seg_tokens, _pipe_before in segments:
        if not seg_tokens:
            continue
        working = _skip_leading_env_assignments(
            _strip_leading_subshell_and_env([normalize(t) for t in seg_tokens])
        )
        if not working:
            continue
        payload = _unwrap_shell_c(working)
        hit = _evaluate(payload) if payload is not None else _classify_segment(working)
        found = _worse(found, hit)
        if found is not None and found[1]:
            return found
    return found


def _evaluate(cmd: str) -> Optional[_Hit]:
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return None
    return _scan_segments(_segments_from_tokens(tokens), lambda t: t)


def _evaluate_powershell(cmd: str) -> Optional[_Hit]:
    segments = resolve_segments_for_dialect(
        cmd, Dialect.POWERSHELL, guard_name="block_venv_creation"
    )
    if segments is None:
        return _evaluate(strip_powershell_prose_noise(cmd))
    return _scan_segments(segments, lambda t: _strip_ps_quotes(t).replace("`", ""))


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    if dialect_from_tool_name(tool_name) is Dialect.POWERSHELL:
        hit = _evaluate_powershell(cmd)
    else:
        hit = _evaluate(_strip_heredoc_bodies(cmd))
    if hit is None:
        return None

    if os.environ.get(_OVERRIDE_ENV) == "1":
        return None

    shape, is_deny = hit
    from coordinator_core.guard_messages.alternative_record import (
        AdaptableOffer,
        AlternativeRecord,
        EscalationRoute,
        Override,
    )

    members = (
        AdaptableOffer(
            template="python3 <the same work on the system interpreter>",
            what_to_adapt="the script",
        ),
        EscalationRoute(
            report_what="the dependency you need isolated",
            authorize_what="a venv",
        ),
    )
    if is_deny:
        summary = "BLOCKED [%s]: `%s` creates a venv. Use the system interpreter." % (_TRIPWIRE, shape)
        return AlternativeRecord(
            verdict_kind="deny",
            guard_name="block-venv-creation",
            summary=summary,
            members=members + (Override(_OVERRIDE_ENV),),
            em_text=_stamp(
                summary
                + "\n\n"
                + operator_override_note(_OVERRIDE_ENV, payload=payload)
            ),
        )
    advisory = "ADVISORY [%s]: `%s` provisions a venv. Use the system interpreter." % (_TRIPWIRE, shape)
    return AlternativeRecord(
        verdict_kind="advisory",
        guard_name="block-venv-creation",
        summary=advisory,
        members=members,
        em_text=advisory,
    )
