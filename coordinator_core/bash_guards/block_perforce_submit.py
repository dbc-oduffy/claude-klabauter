"""coordinator_core.bash_guards.block_perforce_submit -- PreToolUse(Bash,
PowerShell) hard deny: no shell command puts a changelist on Perforce from a
box under the no-submit policy.

Box policy (PM, 2026-10-05): FIFA code may be read, synced, built, run and
edited locally; it is never submitted. A shelve is server-visible, so it is
denied with the submit. The MCP route is DoE's guard; this is the shell route.

Armed by the machine-local key `policy.p4_no_submit` (a list of the box's
depot identifiers). Absent or empty: allow. Armed: deny regardless of which
server or client the command names (fail closed), for
  - `p4 [global opts] <verb>` where verb writes to the server
    (`SERVER_WRITE_VERBS`), and `git p4 submit` / `git-p4 submit`;
  - a p4 command whose subcommand is hidden behind `$(...)`, backticks or a
    variable;
  - an inline P4Python submit/shelve call.
`sync`, `edit`, `revert`, `opened`, `unshelve`, `resolve` and text that only
mentions p4 (`echo "p4 submit"`, `grep "p4 submit"`) are allowed.

No override: the PM made it a hard requirement.
"""

from __future__ import annotations

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

POLICY_KEY = "policy.p4_no_submit"

#: Verbs that write a changelist, or its content, to the server.
SERVER_WRITE_VERBS = frozenset(
    {"submit", "shelve", "reshelve", "populate", "unsubmit", "resubmit", "obliterate"}
)
#: p4 global options that take a value.
_P4_GLOBAL_OPT_WITH_ARG = frozenset(
    {"-c", "-C", "-d", "-H", "-L", "-p", "-P", "-Q", "-r", "-u", "-x", "-z"}
)
_PRE_FILTER_RE = re.compile(r"(?i)\bp4|submit|shelve")
_P4PYTHON_RE = re.compile(
    r"""(?ix)
    \brun_(?:submit|shelve|populate)\b
    | \bsave_submit\b
    | \brun\(\s*['"](?:submit|shelve|reshelve|populate)['"]
    """
)


def _armed() -> bool:
    from coordinator_core.machine_resolver import load_flat_registry_file, registry_dir

    try:
        reg_dir = registry_dir()
        for fname in ("registry.local.toml", "registry.toml"):
            flat = load_flat_registry_file(reg_dir / fname)
            if POLICY_KEY in flat:
                return bool(flat[POLICY_KEY])
    except Exception:  # noqa: BLE001 -- an unreadable registry cannot prove the box unarmed
        return True
    return False


def _hidden(tok: str) -> bool:
    return tok.startswith("$") or "$(" in tok or "`" in tok or tok.startswith("%")


def _p4_offence(tokens: List[str]) -> Optional[str]:
    if not tokens:
        return None
    if token_matches_binary(tokens[0], "git-p4") or (
        token_matches_binary(tokens[0], "git") and len(tokens) > 1 and tokens[1] == "p4"
    ):
        rest = tokens[1:] if token_matches_binary(tokens[0], "git-p4") else tokens[2:]
        return "git p4 submit" if rest and rest[0].lower() == "submit" else None
    if not token_matches_binary(tokens[0], "p4"):
        return None
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in _P4_GLOBAL_OPT_WITH_ARG else 1
    if i >= len(tokens):
        return None
    verb = tokens[i]
    if _hidden(verb):
        return f"p4 {verb} (subcommand not readable)"
    verb = verb.lower()
    return f"p4 {verb}" if verb in SERVER_WRITE_VERBS else None


def _offence(cmd: str) -> Optional[str]:
    for resolved in resolve_command_positions(cmd):
        found = _p4_offence(resolved.tokens)
        if found:
            return found
    m = _P4PYTHON_RE.search(cmd)
    return f"P4Python `{m.group(0)}`" if m else None


def _deny_reason(what: str) -> str:
    return (
        f"BLOCKED: {what} writes to Perforce. Box policy: this depot is read, synced, "
        "built and edited locally, never submitted or shelved. Leave the change "
        "opened locally and hand the changelist to a human."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    offence = _offence(cmd.replace("\r", ""))
    if offence is None or not _armed():
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(offence),
        }
    }
