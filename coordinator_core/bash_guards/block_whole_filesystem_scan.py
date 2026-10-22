"""coordinator_core.bash_guards.block_whole_filesystem_scan -- PreToolUse(Bash,
PowerShell) hard deny: no search rooted at a whole filesystem or drive.

Denied when the search root is `/`, a drive root (`C:`, `C:/`, `C:` plus a backslash, `/c`,
`/c/`, any letter) or the home directory (`~`, `~/`, `$HOME`):
  - `find <root> ...` (any start path before the first expression);
  - `rg`, `fd` with a positional path that is a root;
  - `grep -r` / `-R` / `--recursive` with a root operand.
  - `grep -r` / `-R` whose target (`.` when none) is a repo root (holds `.git`) with a direct
    child in HEAVY_TREE_NAMES (scratch, data, toolchains, node_modules, ...) not covered by an
    `--exclude-dir` glob; one os.scandir, no walk. An unknown cwd with a relative target allows.
A root that appears only inside a pattern or option value (`-path /x/y`,
`-name`, the pattern operand) is not a search root and is allowed.

No override; the heavy-tree case is floor too.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from typing import Any, Dict, List, Optional

from coordinator_core.search.engine import DEFAULT_PRUNE_DIRS

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
HEAVY_TREE_NAMES = (DEFAULT_PRUNE_DIRS - {".git", ".hg", ".svn"}) | {
    "scratch", "scratch-hold", "data", "toolchains", "Intermediate", "Saved", "DerivedDataCache",
}
_PRE_FILTER_RE =re.compile(r"\b(?:find|rg|grep|egrep|fgrep|fd|fdfind)\b")

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


def _recursive_grep_targets(head: str, args: List[str]) -> Optional[List[str]]:
    """Targets of a recursive grep (`.` when none is typed), or None for any other command."""
    if not any(token_matches_binary(head, g) for g in ("grep", "egrep", "fgrep")):
        return None
    pos, seen = _positionals(args, _GREP_VALUE_OPTS)
    recursive = any(o in ("--recursive", "--dereference-recursive") for o in seen) or any(
        a.startswith("-") and not a.startswith("--") and (set(a[1:]) & set("rR"))
        for a in args if a != "--"
    )
    if not recursive:
        return None
    has_pat_opt = any(o in _PATTERN_FROM_OPT["grep"] for o in seen)
    targets = pos if has_pat_opt else pos[1:]
    return targets or ["."]


def _brace_expand(glob: str) -> List[str]:
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    out: List[str] = []
    for alt in m.group(1).split(","):
        out.extend(_brace_expand(glob[:m.start()] + alt + glob[m.end():]))
    return out


def _exclude_globs(args: List[str]) -> List[str]:
    globs: List[str] = []
    for i, a in enumerate(args):
        if a.startswith("--exclude-dir="):
            raw = a.split("=", 1)[1]
        elif a == "--exclude-dir" and i + 1 < len(args):
            raw = args[i + 1]
        else:
            continue
        globs.extend(_brace_expand(raw.strip("\"'")))
    return globs


def _heavy_children(root: str) -> tuple:
    """Sorted heavy-tree directory names directly under a repo root; () for a non-repo or unreadable root."""
    try:
        with os.scandir(root) as it:
            entries = list(it)
        if not any(e.name == ".git" for e in entries):
            return ()
        return tuple(sorted(
            e.name for e in entries if e.name in HEAVY_TREE_NAMES and e.is_dir()
        ))
    except OSError:
        return ()


def _covered(name: str, exclude_globs: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, g.rstrip("/")) for g in exclude_globs)


def _heavy_tree_reason(toks: List[str], cwd: Optional[str], cmd_text: str) -> Optional[str]:
    targets = _recursive_grep_targets(toks[0], toks[1:])
    if targets is None:
        return None
    from coordinator_core.bash_guards._write_bump_sink_shapes import resolve_relative
    from coordinator_core.bash_guards.block_drive_root_new_dir_bash_write import _effective_cwd

    eff = _effective_cwd(cmd_text, cwd)
    globs = _exclude_globs(toks[1:])
    for target in targets:
        if not eff and not (target.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", target)):
            continue
        resolved = resolve_relative(eff or "", target)
        if not resolved:
            continue
        uncovered = [n for n in _heavy_children(resolved) if not _covered(n, globs)]
        if not uncovered:
            continue
        flag = "--exclude-dir=" + (uncovered[0] if len(uncovered) == 1 else "{" + ",".join(uncovered) + "}")
        scoped = " ".join([toks[0], flag] + [shlex.quote(t) for t in toks[1:]])
        return (
            f"BLOCKED: `grep -r` at repo root {target} walks {', '.join(uncovered[:3])} "
            f"and outlives a Bash timeout. Scope it: `{scoped}` or a source subdirectory."
        )
    return None


def _offence(text: str, cwd: Optional[str] = None) -> Optional[str]:
    """The full deny reason for the first offending segment, or None."""
    if not _PRE_FILTER_RE.search(text):
        return None
    clean = text.replace("\r", "")
    for resolved in resolve_command_positions(clean):
        toks = resolved.tokens
        if not toks:
            continue
        for root in _search_roots(toks[0], toks[1:]):
            if _is_root(root):
                return (
                    f"BLOCKED: `{toks[0]} {root}` is a whole-filesystem scan: it walks every mounted "
                    "drive and pins the box. Scope it to a repo or engine directory."
                )
        heavy = _heavy_tree_reason(toks, cwd, clean)
        if heavy:
            return heavy
    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    try:
        reason = _offence(cmd, payload.get("cwd") or None)
    except Exception:
        return None
    if reason is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
