"""coordinator_core.bash_guards.block_empty_source_push -- PreToolUse(Bash,
PowerShell) hard deny: a `git push` that can delete a remote branch by accident.

Denied, from the command text alone (zero spawns, no git config read):
  - `--delete` / `-d`;
  - a refspec `<src>:<dst>` whose source is empty (`:refs/heads/x` is a delete);
  - a source holding `$`, `$(` or a backtick that is not a literal 40-hex sha
    (the variable may expand to empty at run time).

Allowed: `--dry-run` / `-n`, a refspec without `:`, a literal 40-hex source.
Known uncovered shapes: `--mirror` and `--prune`.

Override: `COORDINATOR_OVERRIDE_PUSH_DELETE=1` in the call's env.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import resolve_command_positions
from coordinator_core.bash_guards._rewrite_support import _override
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_topic_branch import (
    _PUSH_DELETE_FLAGS,
    _QUOTED_RE,
    _cd_target,
    _git_argv,
    _push_positional,
)

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 44

ANCHOR = "A-PUSH-WITH-AN-EMPTY-SOURCE-DELETES-A-BRANCH"
OVERRIDE_ENV = "COORDINATOR_OVERRIDE_PUSH_DELETE"

_PRE_FILTER_RE = re.compile(r"\bpush\b")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_DRY_RUN_FLAGS = ("-n", "--dry-run")
_BUNDLED_DELETE_RE = re.compile(r"^-[A-Za-z]*d[A-Za-z]*$")


def _unquote(tok: str) -> str:
    """The refspec with a wrapping pair of quotes dropped."""
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "'\"":
        return tok[1:-1]
    return tok


def _offending_refspec(args: List[str]) -> Optional[str]:
    """The first reason string for a deleting or possibly-empty-source push, else None."""
    if any(a in _DRY_RUN_FLAGS or a.startswith("--dry-run") for a in args):
        return None
    if any(a in _PUSH_DELETE_FLAGS or _BUNDLED_DELETE_RE.match(a) for a in args):
        return "--delete"
    for spec in _push_positional(args)[1:]:
        spec = _unquote(spec)
        if spec.startswith("+"):
            spec = spec[1:]
        if ":" not in spec:
            continue
        src = _unquote(spec.rpartition(":")[0])
        if src == "":
            return spec
        if ("$" in src or "`" in src) and not _SHA_RE.match(src):
            return spec
    return None


def _deny_record(offender: str) -> Any:
    from coordinator_core.guard_messages.alternative_record import AlternativeRecord, EscalationRoute

    summary = f"BLOCKED: `git push` with {offender} can delete a remote branch. {ANCHOR}."
    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="block-empty-source-push",
        summary=summary,
        em_text=f"{summary} Alternative: push an explicit 40-char sha or branch name.",
        members=(
            EscalationRoute(
                report_what="the branch the push was meant to update",
                authorize_what="a human running the delete",
            ),
        ),
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    cmd = cmd.replace("\r", "")
    cwd = payload.get("cwd")
    follow_cd = not any(c in _QUOTED_RE.sub("", cmd) for c in "()")
    for resolved in resolve_command_positions(cmd, preserve_windows_backslashes=True):
        if follow_cd and resolved.depth == 0 and resolved.tokens[:1] == ["cd"]:
            cwd = _cd_target(cwd, resolved.tokens[1:])
            continue
        parsed = _git_argv(resolved.tokens)
        if not (parsed and parsed[0] == "push"):
            continue
        offender = _offending_refspec(parsed[1])
        if offender is None:
            continue
        if _override(OVERRIDE_ENV, payload=payload):
            return None
        return _deny_record(offender)
    return None
