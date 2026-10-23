"""coordinator_core.bash_guards.block_hand_authored_handoff_creation --
PreToolUse(Bash) hard-deny twin of
``write_guards.block_hand_authored_handoff_creation``.

Denies a shell redirect (``>``, ``>>``) or ``tee`` that CREATES a file under
``.claude/handoffs/`` or ``state/handoffs/``. A lexical check over the command
text, never an interpreter: it closes the redirect and ``tee`` shapes, not a
deliberately assembled path. A target that already exists passes, as does a
segment whose command is a sanctioned producer (``baton-assemble``,
``coordinator-doc-new``), which write with ``open()`` rather than a shell
redirect.

Never fails closed on an unexpected error.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    normalize_executable_basename,
    segments_from_tokens_with_pipe_flag,
    tokenize_full_command,
)
from coordinator_core.write_guards import block_hand_authored_handoff_creation as _write_twin

CLASS = "hard-deny"
MATCHERS = ("Bash",)
PRIORITY = 61

_SANCTIONED_PRODUCERS = frozenset({"baton-assemble", "coordinator-doc-new"})

_REDIR_RE = re.compile(r"^\d*>{1,2}(?:&\d*)?")


def _segment_targets(seg: List[str]) -> List[str]:
    """Redirect targets and ``tee`` operands of one command segment."""
    targets: List[str] = []
    n = len(seg)
    for i, tok in enumerate(seg):
        match = _REDIR_RE.match(tok)
        if not match or "&" in match.group(0):
            continue
        remainder = tok[match.end():]
        if remainder:
            targets.append(remainder)
        elif i + 1 < n:
            targets.append(seg[i + 1])
    for i, tok in enumerate(seg):
        if normalize_executable_basename(tok) == "tee":
            targets.extend(t for t in seg[i + 1:] if not t.startswith("-"))
            break
    return targets


def _creates_handoff(cmd: str, cwd: Optional[str]) -> bool:
    tokens = tokenize_full_command(cmd.replace("\\", "/"))
    if not tokens:
        return False
    for seg, _pipe in segments_from_tokens_with_pipe_flag(tokens):
        if any(normalize_executable_basename(t) in _SANCTIONED_PRODUCERS for t in seg):
            continue
        for target in _segment_targets(seg):
            if _write_twin.is_new_handoff_path(target.strip("\"'"), cwd):
                return True
    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_write_twin.OVERRIDE_ENV_VAR, "0") == "1":
            return None
        if (payload.get("tool_name") or "") not in MATCHERS:
            return None
        tool_input = payload.get("tool_input") or {}
        cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
        if not cmd or "handoffs" not in cmd:
            return None
        if not _creates_handoff(cmd.replace("\r", ""), payload.get("cwd")):
            return None
        return _deny_record(payload)
    except Exception:
        return None


def _deny_record(payload: Dict[str, Any]) -> Any:
    """The write twin's reason text as a record; the twin itself is untouched."""
    from coordinator_core.guard_messages.alternative_record import (
        AlternativeRecord,
        EscalationRoute,
        Override,
        ReadyCommand,
    )

    summary, _, offer = _write_twin.DENY_REASON.partition("\nUse instead: ")
    command = offer.strip().strip("`")
    members: List[Any] = [ReadyCommand(text=command)] if command else []
    members.append(
        EscalationRoute(
            report_what="why the sanctioned producers cannot create this handoff",
            authorize_what="a hand-authored handoff",
        )
    )
    members.append(Override(env_var=_write_twin.OVERRIDE_ENV_VAR))
    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="block-hand-authored-handoff-creation",
        summary=summary,
        em_text=_write_twin.deny_envelope(payload)["hookSpecificOutput"]["permissionDecisionReason"],
        members=tuple(members),
    )
