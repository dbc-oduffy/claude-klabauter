"""coordinator_core.bash_guards.block_whole_filesystem_scan -- PreToolUse(Bash,
PowerShell) hard deny: no search rooted at a whole filesystem or drive.

Denied when the search root is `/`, a drive root (`C:`, `C:/`, `C:` plus a backslash, `/c`,
`/c/`, any letter) or the home directory (`~`, `~/`, `$HOME`):
  - `find <root> ...` (any start path before the first expression);
  - `rg`, `fd` with a positional path that is a root;
  - `grep -r` / `-R` / `--recursive` with a root operand.
  - recursive `grep`/`rg`, `find -exec` or `du` over a UE install root (`UE_5.x`), its
    Engine/Plugins or Engine/Source subtree, or a `scratch/` root; one run dir under scratch allows.
A root that appears only inside a pattern or option value (`-path /x/y`,
`-name`, the pattern operand) is not a search root and is allowed.

A repo-root `grep -r`/`rg`/`find` is not a whole-drive scan: it allows here and is handled by
`rewrite_repo_root_search` (git grep rewrite or advisory).
"""

from __future__ import annotations

import fnmatch
import re
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
_PRE_FILTER_RE = re.compile(r"\b(?:find|rg|grep|egrep|fgrep|fd|fdfind|du)\b")

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


def _covered(name: str, exclude_globs: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, g.rstrip("/")) for g in exclude_globs)


_UE_ROOT_SEG_RE = re.compile(r"^ue_\d+(?:\.\d+)*$")
_DU_VALUE_OPTS = frozenset({"-d", "-B", "-t", "--max-depth", "--block-size", "--threshold", "--exclude", "-X", "--exclude-from", "--files0-from"})


def _huge_tree_kind(path: str) -> Optional[str]:
    """`engine` for a UE install root or its Engine/Plugins/Source subtree, `scratch` for a scratch/ root, else None."""
    segs = [s for s in path.replace("\\", "/").lower().split("/") if s and s != "."]
    if segs and segs[-1] in ("scratch", "scratch-hold"):
        return "scratch"
    for i, s in enumerate(segs):
        if _UE_ROOT_SEG_RE.match(s):
            rest = segs[i + 1:]
            if not rest or rest == ["engine"] or rest[:2] in (["engine", "plugins"], ["engine", "source"]):
                return "engine"
    if segs and segs[-1] == "epic games":
        return "engine"
    return None


def _huge_scan_targets(head: str, args: List[str]) -> Optional[List[str]]:
    """Search paths of a recursive grep/rg, a `find -exec`, or a `du`; None for any other command."""
    rec = _recursive_grep_targets(head, args)
    if rec is not None:
        return rec
    if token_matches_binary(head, "rg"):
        pos, seen = _positionals(args, _RG_VALUE_OPTS)
        paths = pos if any(o in _PATTERN_FROM_OPT["rg"] for o in seen) else pos[1:]
        return paths or ["."]
    if token_matches_binary(head, "find"):
        if not any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args):
            return None
        return _find_roots(args) or ["."]
    if token_matches_binary(head, "du"):
        pos, _ = _positionals(args, _DU_VALUE_OPTS)
        return pos or ["."]
    return None


def _huge_tree_reason(toks: List[str], cwd: Optional[str], cmd_text: str) -> Any:
    targets = _huge_scan_targets(toks[0], toks[1:])
    if targets is None:
        return None
    from coordinator_core.bash_guards._write_bump_sink_shapes import resolve_relative
    from coordinator_core.bash_guards.block_drive_root_new_dir_bash_write import _effective_cwd
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    eff = _effective_cwd(cmd_text, cwd)
    for target in targets:
        path = target
        if eff and not (target.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", target)):
            path = resolve_relative(eff, target) or target
        kind = _huge_tree_kind(path)
        if kind == "engine":
            return _record(
                f"BLOCKED: `{toks[0]}` over {target} walks the UE engine tree and floods the file cache.",
                " Use project_engine_type_hierarchy, project_cpp_symbol or project_engine_document_symbols.",
                AdaptableOffer(template="example-retrieval-repo engine tool for the symbol", what_to_adapt="the symbol"),
            )
        if kind == "scratch":
            return _record(
                f"BLOCKED: `{toks[0]}` over scratch root {target} reads every run's output and floods the file cache.",
                " Scope it to one run dir, e.g. scratch/<run>.",
                AdaptableOffer(template="scan one scratch run dir", what_to_adapt="the run dir"),
            )
    return None


def _offence(text: str, cwd: Optional[str] = None) -> Any:
    """The deny record for the first offending segment, or None."""
    if not _PRE_FILTER_RE.search(text):
        return None
    clean = text.replace("\r", "")
    for resolved in resolve_command_positions(clean):
        toks = resolved.tokens
        if not toks:
            continue
        for root in _search_roots(toks[0], toks[1:]):
            if _is_root(root):
                from coordinator_core.guard_messages.alternative_record import AdaptableOffer

                return _record(
                    f"BLOCKED: `{toks[0]} {root}` is a whole-filesystem scan: it walks every mounted "
                    "drive and pins the box.",
                    " Scope it to a repo or engine directory.",
                    AdaptableOffer(template="scan a repo or engine directory", what_to_adapt="the scan root"),
                )
        huge = _huge_tree_reason(toks, cwd, clean)
        if huge:
            return huge
    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    try:
        return _offence(cmd, payload.get("cwd") or None)
    except Exception:
        return None


def _record(summary: str, tail: str, *members: Any) -> Any:
    from coordinator_core.guard_messages.alternative_record import AlternativeRecord

    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="block-whole-filesystem-scan",
        summary=summary,
        em_text=summary + tail,
        members=members,
    )
