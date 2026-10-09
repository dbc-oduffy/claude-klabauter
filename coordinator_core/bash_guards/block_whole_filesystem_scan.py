"""coordinator_core.bash_guards.block_whole_filesystem_scan -- PreToolUse(Bash,
PowerShell) hard deny: no search rooted at a whole filesystem or drive.

Denied when the search root is `/`, a drive root (`C:`, `C:/`, `C:` plus a backslash, `/c`,
`/c/`, any letter) or the home directory (`~`, `~/`, `$HOME`):
  - `find <root> ...` (any start path before the first expression);
  - `rg`, `fd` with a positional path that is a root;
  - `grep -r` / `-R` / `--recursive` with a root operand.
A root that appears only inside a pattern or option value (`-path /x/y`,
`-name`, the pattern operand) is not a search root and is allowed.

No override.
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

_ROOT_RE = re.compile(
    r"^(?:[/\\]{1,2}|[/\\][A-Za-z][/\\]?|[A-Za-z]:[/\\]*|[A-Za-z]:\s.*|~[/\\]?|\$HOME[/\\]?|\$\{HOME\}[/\\]?)$"
)
_PRE_FILTER_RE = re.compile(r"\b(?:find|rg|grep|egrep|fgrep|fd|fdfind)\b")

_RG_VALUE_OPTS = frozenset({
    "-e", "-f", "-g", "-t", "-T", "-A", "-B", "-C", "-m", "-j", "-d", "-r", "-E",
    "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--type-add",
    "--after-context", "--before-context", "--context", "--max-count", "--threads",
    "--max-depth", "--replace", "--encoding", "--max-filesize", "--sort", "--sortr",
    "--colors", "--engine", "--ignore-file", "--pre", "--pre-glob",
})
_GREP_VALUE_OPTS = frozenset({
    "-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D",
    "--regexp", "--file", "--max-count", "--after-context", "--before-context",
    "--context", "--include", "--exclude", "--exclude-dir", "--exclude-from",
    "--directories", "--devices", "--label", "--binary-files",
})
_FD_VALUE_OPTS = frozenset({
    "-e", "-E", "-t", "-d", "-S", "-c", "-j", "-o", "-C", "--extension", "--exclude",
    "--type", "--max-depth", "--min-depth", "--exact-depth", "--size", "--color",
    "--threads", "--owner", "--changed-within", "--changed-before", "--base-directory",
    "--ignore-file", "--max-results", "--path-separator",
})
_PATTERN_FROM_OPT = {
    "rg": frozenset({"-e", "-f", "--regexp", "--file"}),
    "grep": frozenset({"-e", "-f", "--regexp", "--file"}),
    "fd": frozenset(),
}
_FIND_LEADING_OPTS = frozenset({"-H", "-L", "-P", "-E", "-X", "-x", "-s", "-d", "-f"})


def _is_root(tok: str) -> bool:
    return bool(_ROOT_RE.match(tok))


def _positionals(args: List[str], value_opts: frozenset, cluster_value: str = "") -> tuple:
    """Return (positionals, saw_pattern_opt_flags) skipping option values."""
    pos: List[str] = []
    seen_opts: List[str] = []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            seen_opts.append(name)
            if "=" not in a and name in value_opts:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            seen_opts.append(a[:2])
            for k, ch in enumerate(a[1:], start=1):
                if "-" + ch in value_opts:
                    seen_opts.append("-" + ch)
                    if k == len(a) - 1:
                        i += 1
                    break
        else:
            pos.append(a)
        i += 1
    return pos, seen_opts


def _find_roots(args: List[str]) -> List[str]:
    roots: List[str] = []
    for a in args:
        if a in _FIND_LEADING_OPTS and not roots:
            continue
        if a.startswith("-") or a in ("(", "!", ")"):
            break
        roots.append(a)
    return roots


def _search_roots(head: str, args: List[str]) -> List[str]:
    if token_matches_binary(head, "find"):
        return _find_roots(args)
    if token_matches_binary(head, "rg"):
        name, opts = "rg", _RG_VALUE_OPTS
    elif token_matches_binary(head, "fd") or token_matches_binary(head, "fdfind"):
        name, opts = "fd", _FD_VALUE_OPTS
    elif any(token_matches_binary(head, g) for g in ("grep", "egrep", "fgrep")):
        name, opts = "grep", _GREP_VALUE_OPTS
    else:
        return []
    pos, seen = _positionals(args, opts)
    if name == "grep":
        recursive = any(o in ("--recursive", "--dereference-recursive") for o in seen) or any(
            a.startswith("-") and not a.startswith("--") and (set(a[1:]) & set("rR"))
            for a in args if a != "--"
        )
        if not recursive:
            return []
    has_pat_opt = any(o in _PATTERN_FROM_OPT[name] for o in seen)
    if name == "fd":
        paths = pos[1:] if pos else []
        for i, a in enumerate(args):
            if a == "--search-path" and i + 1 < len(args):
                paths.append(args[i + 1])
        return paths
    return pos if has_pat_opt else pos[1:]


def _offence(text: str) -> Optional[str]:
    if not _PRE_FILTER_RE.search(text):
        return None
    for resolved in resolve_command_positions(text.replace("\r", "")):
        toks = resolved.tokens
        if not toks:
            continue
        for root in _search_roots(toks[0], toks[1:]):
            if _is_root(root):
                return f"{toks[0]} {root}"
    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    try:
        offence = _offence(cmd)
    except Exception:
        return None
    if offence is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"BLOCKED: `{offence}` is a whole-filesystem scan: it walks every mounted "
                "drive and pins the box. Scope it to a repo or engine directory, or use "
                "the code-index tools (project_file, project_cpp_symbol)."
            ),
        }
    }
