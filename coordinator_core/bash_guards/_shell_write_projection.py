"""Which in-repo paths a Bash command writes, and with what text when the text is literal.

`shell_writes` applies the same filter the touch recorder applies
(`write_claim_record.record_write_claims`) so the XS lane budget and the commit
claim see the same target set. Reads only `cmd`; no subprocess.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Dict, List, NamedTuple, Optional

from coordinator_core.bash_guards.write_claim_record import (
    _is_bare_heredoc_opener,
    _is_claimable_target,
    _rel_if_inside,
)


class ShellWrite(NamedTuple):
    """`body` is the literal heredoc text (each line newline-terminated), or None
    when the text is not statically known."""

    abs_path: str
    body: Optional[str]
    append: bool


_HEREDOC_OPENER_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_SEGMENT_SPLIT_RE = re.compile(r"&&|;")


def _literal_heredoc(cmd: str):
    """Return `(head_line, body)` for a command with exactly one heredoc whose body
    is literal and nothing after its terminator, else None."""
    openers = list(_HEREDOC_OPENER_RE.finditer(cmd))
    if len(openers) != 1 or cmd.count("<<") != 1:
        return None
    m = openers[0]
    strip_tabs = bool(m.group(1))
    quoted = m.group(4) is None or "\\" in m.group(0)
    delim = m.group(2) or m.group(3) or m.group(4)
    head = cmd[: m.start()]
    rest = cmd[m.end() :]
    nl = rest.find("\n")
    if nl < 0:
        return None
    if rest[:nl].strip():
        return None
    lines = rest[nl + 1 :].split("\n")
    body_lines: List[str] = []
    end = None
    for i, line in enumerate(lines):
        cand = line.lstrip("\t") if strip_tabs else line
        if cand == delim:
            end = i
            break
        body_lines.append(cand if strip_tabs else line)
    if end is None or any(ln.strip() for ln in lines[end + 1 :]):
        return None
    body = "".join(ln + "\n" for ln in body_lines)
    if not quoted and ("$" in body or "`" in body):
        return None
    return head, body


def _body_targets(head: str) -> Dict[str, bool]:
    """Raw target tokens `{raw: append}` of a single `cat`/`tee` segment (optionally
    preceded by `cd` segments); empty for any other shape."""
    if "|" in head:
        return {}
    segs = _SEGMENT_SPLIT_RE.split(head)
    if not segs or any(s.strip().split(None, 1)[:1] != ["cd"] for s in segs[:-1] if s.strip()):
        return {}
    try:
        toks = shlex.split(segs[-1])
    except ValueError:
        return {}
    if not toks or os.path.basename(toks[0]) not in ("cat", "tee"):
        return {}
    out: Dict[str, bool] = {}
    if os.path.basename(toks[0]) == "tee":
        append = any(t in ("-a", "--append") for t in toks[1:])
        for t in toks[1:]:
            if not t.startswith("-"):
                out[t] = append
        return out
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in (">", ">>") and i + 1 < len(toks):
            out[toks[i + 1]] = t == ">>"
            i += 2
            continue
        if t.startswith(">") and len(t) > 1 and not t.startswith(">&"):
            out[t.lstrip(">")] = t.startswith(">>")
        i += 1
    return out


_SEPARATORS = frozenset({"&&", ";", "||", "|", "&", ";;", "|&"})
_COMMIT_VALUE_LONG = frozenset(
    {
        "--message", "--file", "--author", "--date", "--reuse-message", "--reedit-message",
        "--fixup", "--squash", "--cleanup", "--template", "--trailer",
    }
)
_COMMIT_VALUE_SHORT = frozenset("mFCct")
_APPLY_VALUE_LONG = frozenset({"--directory", "--include", "--exclude", "--whitespace"})
_APPLY_NO_WRITE = frozenset({"--check", "--stat", "--numstat", "--summary"})
_PATCH_READ_CAP = 1 << 20


def _git_segments(cmd: str) -> List[List[str]]:
    """Token lists of each simple command; redirections dropped. Empty on a parse failure."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return []
    segs: List[List[str]] = [[]]
    skip = False
    for t in toks:
        if t in _SEPARATORS:
            segs.append([])
            skip = False
            continue
        if skip:
            skip = False
            continue
        if t and (t[0] in "<>" or t.startswith("&>")):
            skip = t in (">", ">>", "<", "<<", "<<<", ">|", "&>", "&>>", ">&", "<&")
            if segs[-1] and segs[-1][-1].isdigit():
                segs[-1].pop()
            continue
        segs[-1].append(t)
    return segs


def _apply_patch_targets(patch: str, strip: int) -> Optional[List[str]]:
    """Destination paths named by a patch file's `+++` headers, or None if unreadable."""
    try:
        with open(patch, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(_PATCH_READ_CAP)
    except OSError:
        return None
    out: List[str] = []
    for line in text.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].split("\t", 1)[0].strip()
        if not path or path == "/dev/null":
            continue
        parts = path.split("/")
        if strip:
            parts = parts[strip:]
        if parts:
            out.append("/".join(parts))
    return out


def _git_segment_specs(toks: List[str], cwd: str, root: str) -> List[str]:
    """Absolute paths a single `git ...` segment writes (root when the set is unknowable)."""
    i = 1
    gcwd = cwd
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-C" and i + 1 < len(toks):
            gcwd = os.path.normpath(os.path.join(gcwd, toks[i + 1]))
            i += 2
        elif toks[i] in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(toks):
            i += 2
        else:
            i += 1
    if i >= len(toks):
        return []
    sub, args = toks[i], toks[i + 1 :]

    def absp(p: str) -> str:
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(gcwd, p))

    specs: List[str] = []
    if sub == "add":
        dd = False
        whole = False
        for a in args:
            if not dd and a == "--":
                dd = True
            elif not dd and a.startswith("--pathspec-from-file"):
                whole = True
            elif not dd and a in ("-A", "--all", "-u", "--update"):
                whole = True
            elif not dd and a.startswith("-"):
                continue
            else:
                specs.append(a)
        return [absp(s) for s in specs] if specs else ([root] if whole else [])
    if sub == "checkout":
        if "--" not in args:
            return []
        return [absp(s) for s in args[args.index("--") + 1 :]]
    if sub == "restore":
        staged_only = any(a in ("--staged", "-S") for a in args) and not any(
            a in ("--worktree", "-W") for a in args
        )
        if staged_only:
            return []
        dd = False
        skip = False
        for a in args:
            if skip:
                skip = False
            elif dd:
                specs.append(a)
            elif a == "--":
                dd = True
            elif a in ("-s", "--source"):
                skip = True
            elif a.startswith("--pathspec-from-file"):
                return [root]
            elif not a.startswith("-"):
                specs.append(a)
        return [absp(s) for s in specs] or [root]
    if sub == "commit":
        every = False
        dd = False
        skip = False
        for a in args:
            if skip:
                skip = False
            elif dd:
                specs.append(a)
            elif a == "--":
                dd = True
            elif a in ("--all", "--amend"):
                every = True
            elif a in _COMMIT_VALUE_LONG:
                skip = True
            elif a.startswith("--") or a == "-":
                continue
            elif a.startswith("-"):
                for k, ch in enumerate(a[1:], 1):
                    if ch == "a":
                        every = True
                    if ch in _COMMIT_VALUE_SHORT:
                        skip = k == len(a) - 1
                        break
            else:
                specs.append(a)
        if every or not specs:
            # No pathspec: the staged set is unknowable without a git spawn, so it counts as the root.
            return [root]
        return [absp(s) for s in specs]
    if sub == "apply":
        if any(a in _APPLY_NO_WRITE for a in args) or (
            "--cached" in args and "--index" not in args
        ):
            return []
        strip = 1
        patches: List[str] = []
        skip = False
        for k, a in enumerate(args):
            if skip:
                skip = False
            elif a == "-p" and k + 1 < len(args):
                skip = True
                strip = int(args[k + 1]) if args[k + 1].isdigit() else 1
            elif a.startswith("-p") and a[2:].isdigit():
                strip = int(a[2:])
            elif a in _APPLY_VALUE_LONG or a in ("-C",):
                skip = True
            elif a.startswith("-") and a != "-":
                continue
            else:
                patches.append(a)
        if not patches or "-" in patches:
            return [root]
        for p in patches:
            names = _apply_patch_targets(absp(p), strip)
            if names is None:
                return [root]
            specs.extend(absp(n) for n in names)
        return specs
    return []


def _git_writes(cmd: str, cwd: str, root: str) -> List[str]:
    paths: List[str] = []
    cur = cwd
    for toks in _git_segments(cmd):
        if not toks:
            continue
        if toks[0] == "cd" and len(toks) > 1:
            cur = os.path.normpath(toks[1] if os.path.isabs(toks[1]) else os.path.join(cur, toks[1]))
        elif os.path.basename(toks[0]) == "git":
            paths.extend(_git_segment_specs(toks, cur, root))
    return paths


def shell_writes(cmd: str, cwd: str, root: str, with_git: bool = False) -> List[ShellWrite]:
    """Every in-repo write target of `cmd`, deduplicated, in encounter order. `with_git` adds the
    pathspecs of `git add/commit/checkout --/restore/apply`; the pre-fire XS budget leaves it off,
    since those targets carry no projectable body and would be denied as unmeasurable."""
    if not cmd or not cmd.strip() or not root:
        return []
    try:
        from coordinator_core.bash_guards.bump_outside_repo_write import (
            _iter_write_sink_candidates,
        )

        lit = _literal_heredoc(cmd)
        body_for = _body_targets(lit[0]) if lit else {}
        out: Dict[str, ShellWrite] = {}
        skip_next = False
        for resolved, head_base, raw in _iter_write_sink_candidates(cmd, cwd):
            if skip_next:
                skip_next = False
                continue
            if _is_bare_heredoc_opener(raw):
                skip_next = True
                continue
            if not _is_claimable_target(raw, head_base, resolved):
                continue
            if _rel_if_inside(resolved, root) is None or resolved in out:
                continue
            if lit and raw in body_for and head_base in ("cat", "tee"):
                out[resolved] = ShellWrite(resolved, lit[1], body_for[raw])
            else:
                out[resolved] = ShellWrite(resolved, None, False)
        if with_git and "git" in cmd:
            for p in _git_writes(cmd, cwd, root):
                if _rel_if_inside(p, root) is not None and p not in out:
                    out[p] = ShellWrite(p, None, False)
        return list(out.values())
    except Exception:  # noqa: BLE001 -- best-effort, never raise into a guard
        return []
