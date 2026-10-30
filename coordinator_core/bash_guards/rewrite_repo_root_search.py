"""coordinator_core.bash_guards.rewrite_repo_root_search -- PreToolUse(Bash, PowerShell)
ADVISORY_REWRITE leg: a recursive search rooted at a repo root (the target, `.` when none, holds
`.git`) is rewritten to `git grep` (tracked files only) when every flag maps; otherwise it is
allowed with an advisory. Never denies.

Per pipeline segment:
  - `grep -r` / `rg` with flags in {-n -i -l -w -F -e, and -E for grep} and at most one target ->
    `git grep` (`git -C <target> grep` when the target is not the cwd);
  - any other `grep -r` / `rg` at a repo root -> advisory; a `grep -r` also gets `--exclude-dir`
    for uncovered heavy-tree children (scratch, data, toolchains, node_modules, ...);
  - `find` from a repo root without `-maxdepth` -> advisory.
A scoped target, an unknown cwd with a relative target, or a non-repo target allows silently.
One `.git` stat per target; no walk.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import token_matches_binary
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_whole_filesystem_scan import (
    HEAVY_TREE_NAMES,
    _covered,
    _exclude_globs,
    _find_roots,
    _recursive_grep_targets,
    _search_roots,
)

CLASS = "advisory-rewrite"
MATCHERS = COMMAND_TOOL_NAMES
LEG_ID = "rewrite-repo-root-search"

ADVISORY = "use `git grep` (tracked files only) or the Grep tool"
REWRITE_NOTE = "Repo-root search rewritten to `git grep`: it now covers tracked files only; use the Grep tool for untracked paths."

_PRE_FILTER_RE = re.compile(r"\b(?:find|rg|grep|egrep|fgrep)\b")
_COMPLEX_RE = re.compile(r"[<>$`()]")
_ERE_ONLY_RE = re.compile(r"[|+?(){}]")
_RG_ONLY_RE = re.compile(r"\\[dDsSpPuUx]|\(\?")
_GREP_HEADS = ("grep", "egrep", "fgrep")


def _segments(cmd: str) -> List[Tuple[int, int]]:
    """Spans of `cmd` between unquoted `| ; & newline`, empty spans dropped."""
    spans: List[Tuple[int, int]] = []
    start = 0
    quote = ""
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c in "|;&\n":
            if cmd[start:i].strip():
                spans.append((start, i))
            start = i + 1
        i += 1
    if cmd[start:].strip():
        spans.append((start, n))
    return spans


def _map_to_git_grep(head: str, args: List[str]) -> Optional[Tuple[List[str], List[str], List[str], List[str]]]:
    """(flag letters, patterns, targets, pathspecs) when every flag has a faithful `git grep` twin, else None.

    grep's `--include=G` / `--exclude-dir=D` become glob pathspecs: left unmapped, the excluding form
    fell to the advisory and still walked the whole tree."""
    is_rg = token_matches_binary(head, "rg")
    ok = "nilwF" if is_rg else "nilwFErRI"
    if token_matches_binary(head, "egrep"):
        return None
    if token_matches_binary(head, "fgrep"):
        return None
    flags: List[str] = []
    patterns: List[str] = []
    pos: List[str] = []
    specs: List[str] = []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if is_rg:
                return None
            if name == "--include" and eq and value:
                specs.append(f":(glob)**/{value}")
            elif name == "--exclude-dir" and eq and value:
                specs.append(f":(exclude,glob)**/{value.strip('/')}/**")
            elif a != "--recursive":
                return None
        elif a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            for k, ch in enumerate(cluster):
                if ch == "e":
                    if k != len(cluster) - 1 or i + 1 >= n:
                        return None
                    patterns.append(args[i + 1])
                    i += 1
                elif ch not in ok:
                    return None
                elif ch not in "rR" and ch not in flags:
                    flags.append(ch)
        else:
            pos.append(a)
        i += 1
    if not patterns:
        if not pos:
            return None
        patterns, pos = [pos[0]], pos[1:]
    if is_rg and "F" not in flags:
        if any(_RG_ONLY_RE.search(p) for p in patterns):
            return None
        if "E" not in flags and any(_ERE_ONLY_RE.search(p) for p in patterns):
            flags.append("E")
    return flags, patterns, pos, specs


def _repo_root_of(target: str, eff: Optional[str]) -> Optional[str]:
    """The resolved target when it holds `.git`; None for a scoped, non-repo or unresolvable one."""
    from coordinator_core.bash_guards._write_bump_sink_shapes import resolve_relative

    if not eff and not (target.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", target)):
        return None
    resolved = resolve_relative(eff or "", target)
    if resolved and os.path.exists(os.path.join(resolved, ".git")):
        return resolved
    return None


def _heavy_children(root: str) -> Tuple[str, ...]:
    try:
        with os.scandir(root) as it:
            return tuple(sorted(e.name for e in it if e.name in HEAVY_TREE_NAMES and e.is_dir()))
    except OSError:
        return ()


def _same_dir(a: str, b: Optional[str]) -> bool:
    return bool(b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _plan_segment(body: str, eff: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """(kind, replacement body) for one segment: kind `rewrite` carries the new text, `advise` may
    carry an `--exclude-dir` text edit; None when the segment is not a repo-root search."""
    if not _PRE_FILTER_RE.search(body):
        return None
    try:
        toks = shlex.split(body)
    except ValueError:
        return None
    if not toks:
        return None
    head, args = toks[0], toks[1:]
    simple = not _COMPLEX_RE.search(body)

    if token_matches_binary(head, "find"):
        if "-maxdepth" in args:
            return None
        roots = _find_roots(args) or ["."]
        return ("advise", None) if any(_repo_root_of(r, eff) for r in roots) else None

    is_rg = token_matches_binary(head, "rg")
    if is_rg:
        targets = _search_roots(head, args) or ["."]
    else:
        targets = _recursive_grep_targets(head, args)
        if targets is None:
            return None
    roots = [r for r in (_repo_root_of(t, eff) for t in targets) if r]
    if not roots:
        return None

    mapped = _map_to_git_grep(head, args) if simple and len(targets) == 1 else None
    if mapped is not None:
        flags, patterns, tgt, specs = mapped
        if len(tgt) > 1 or (tgt and tgt[0].startswith("~")):
            mapped = None
    if mapped is not None:
        flags, patterns, tgt, specs = mapped
        base = "git" if not tgt or tgt[0].rstrip("/\\") in ("", ".") or _same_dir(roots[0], eff) \
            else "git -C " + shlex.quote(tgt[0])
        parts = [base, "grep"]
        if flags:
            parts.append("-" + "".join(flags))
        for p in patterns:
            if len(patterns) > 1 or p.startswith("-"):
                parts += ["-e", shlex.quote(p)]
            else:
                parts.append(shlex.quote(p))
        if specs:
            parts += ["--"] + [shlex.quote(s) for s in specs]
        return "rewrite", " ".join(parts)

    if is_rg or not simple or body.split(None, 1)[0] != head:
        return "advise", None
    globs = _exclude_globs(args)
    uncovered = sorted({n for r in roots for n in _heavy_children(r) if not _covered(n, globs)})
    if not uncovered:
        return "advise", None
    flag = "--exclude-dir=" + (uncovered[0] if len(uncovered) == 1 else "{" + ",".join(uncovered) + "}")
    return "advise", head + " " + flag + body[len(head):]


def check(payload: Dict[str, Any]) -> Any:
    """AlternativeRecord (`rewrite` or `advisory`) for a repo-root recursive search, else None."""
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    try:
        return _check(payload, tool_input, cmd.replace("\r", ""))
    except Exception:  # noqa: BLE001 -- an advisory leg never turns a bug into a verdict
        return None


def _check(payload: Dict[str, Any], tool_input: Dict[str, Any], cmd: str) -> Any:
    from coordinator_core.bash_guards.block_drive_root_new_dir_bash_write import _effective_cwd
    from coordinator_core.guard_messages.alternative_record import (
        AdaptableOffer,
        AlternativeRecord,
        ReadyCommand,
    )

    eff = _effective_cwd(cmd, payload.get("cwd") or None)
    pieces: List[str] = []
    last = 0
    rewrote = advised = False
    for s, e in _segments(cmd):
        raw = cmd[s:e]
        body = raw.strip()
        plan = _plan_segment(body, eff)
        if plan is None:
            continue
        kind, new_body = plan
        if kind == "rewrite":
            rewrote = True
        else:
            advised = True
        if new_body is None:
            continue
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        pieces.append(cmd[last:s] + lead + new_body + trail)
        last = e
    if not (rewrote or advised):
        return None
    new_cmd = "".join(pieces) + cmd[last:] if pieces else cmd
    if rewrote:
        text = REWRITE_NOTE + ("" if not advised else f" Other repo-root searches here: {ADVISORY}.")
    else:
        text = f"Repo-root search walks untracked scratch and data trees: {ADVISORY}."
    if new_cmd != cmd:
        return AlternativeRecord(
            verdict_kind="rewrite",
            summary=text,
            members=(ReadyCommand(text=new_cmd),),
            guard_name=LEG_ID,
            updated_input={**tool_input, "command": new_cmd},
            em_text=text,
        )
    return AlternativeRecord(
        verdict_kind="advisory",
        summary=text,
        members=(AdaptableOffer(template="git grep or the Grep tool", what_to_adapt="the query"),),
        guard_name=LEG_ID,
        em_text=text,
    )
