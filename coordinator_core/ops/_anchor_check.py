"""In-process evaluator for an anchor premise's check. No spawn, no walk: it reads only the
files the command names, under the repo root it is handed.

Grammar (shlex, posix): stage := grep FLAGS (PATTERN | (-e PATTERN)+) FILE, FLAGS = c plus at most
one of E or F (a line counts when any -e pattern matches);
command := stage ("&&" stage)*; expect := N | >=N | <=N | >N | <N | "N then M [then K ...]".
Anything else is NOT_EVALUABLE."""

import re
import shlex
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

MAX_BYTES: int = 1 << 20

_GLOB = set("*?[]{}")
_BRE_UNESCAPE = set("|(){}")
_BRE_ESCAPE = set("(){}|+?")
_OPS = (">=", "<=", ">", "<")
# Python re has no timeout, so catastrophic-backtracking shapes are refused before they can run
# over a MAX_BYTES file: a quantified group holding a quantifier, and a quantified alternation
# group unless its branches are plain literals with pairwise distinct first characters.
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")
_QUANTIFIED_GROUP = re.compile(r"\(([^()]*)\)[+*{]")
_PLAIN_BRANCH = re.compile(r"[^\\.\[\]()*+?{}^$|]+")


def _ambiguous_alternation(compiled: str) -> bool:
    for match in _QUANTIFIED_GROUP.finditer(compiled):
        body = match.group(1)
        if "|" not in body:
            continue
        branches = body.split("|")
        if not all(_PLAIN_BRANCH.fullmatch(b) for b in branches):
            return True
        if len({b[0] for b in branches}) != len(branches):
            return True
    return False


class _NotEvaluable(Exception):
    pass


def _result(state: str, observed: str = "", detail: str = "") -> dict:
    return {"state": state, "observed": observed, "detail": detail}


def _tokens(command: str) -> List[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars="&|;<>()")
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError as exc:
        raise _NotEvaluable(f"unparsable command: {exc}")


def _parse_stages(command: str) -> List[Tuple[str, List[str], str]]:
    toks = _tokens(command)
    if not toks:
        raise _NotEvaluable("empty command")
    stages: List[List[str]] = [[]]
    for tok in toks:
        if tok == "&&":
            stages.append([])
        elif re.fullmatch(r"[&|;<>()]+", tok):
            raise _NotEvaluable(f"unsupported shell operator {tok!r}")
        else:
            stages[-1].append(tok)
    return [_parse_stage(s) for s in stages]


def _parse_stage(words: List[str]) -> Tuple[str, List[str], str]:
    if not words or words[0] != "grep":
        raise _NotEvaluable("only grep stages are evaluable")
    flags: List[str] = []
    patterns: List[str] = []
    rest = words[1:]
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag == "-e":
            if not rest:
                raise _NotEvaluable("-e needs a pattern")
            patterns.append(rest.pop(0))
            continue
        body = flag[1:]
        if not body or not set(body) <= set("cEF"):
            raise _NotEvaluable(f"unsupported grep flag {flag!r}")
        flags.extend(body)
    if len(flags) != len(set(flags)) or "c" not in flags:
        raise _NotEvaluable("flags must include c once, with at most one of E or F")
    if "E" in flags and "F" in flags:
        raise _NotEvaluable("E and F are mutually exclusive")
    if patterns:
        if len(rest) != 1:
            raise _NotEvaluable("a stage with -e patterns takes exactly one file")
    else:
        if len(rest) != 2:
            raise _NotEvaluable("a stage takes exactly one pattern and one file")
        patterns.append(rest.pop(0))
    mode = "F" if "F" in flags else "E" if "E" in flags else "B"
    return mode, patterns, rest[0]


def _compile(mode: str, pattern: str) -> "re.Pattern[str]":
    if mode == "F":
        return re.compile(re.escape(pattern))
    if "[[:" in pattern or "[[." in pattern or "[[=" in pattern:
        raise _NotEvaluable("POSIX bracket classes are not evaluable")
    out: List[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            if i + 1 >= len(pattern):
                raise _NotEvaluable("trailing backslash in pattern")
            nxt = pattern[i + 1]
            i += 2
            if nxt.isalnum():
                raise _NotEvaluable(f"backslash class \\{nxt} is not evaluable")
            if nxt in "<>`'" or (mode == "B" and nxt in "+?"):
                raise _NotEvaluable(f"GNU escape \\{nxt} is not evaluable")
            if mode == "B" and nxt in _BRE_UNESCAPE:
                out.append(nxt)
            else:
                out.append(re.escape(nxt))
            continue
        if mode == "B" and ch in _BRE_ESCAPE:
            out.append("\\" + ch)
        else:
            out.append(ch)
        i += 1
    compiled = "".join(out)
    if _NESTED_QUANTIFIER.search(compiled) or _ambiguous_alternation(compiled):
        raise _NotEvaluable("nested quantifier is not evaluable (backtracking risk)")
    try:
        return re.compile(compiled)
    except re.error as exc:
        raise _NotEvaluable(f"pattern does not compile: {exc}")


def _resolve(file_arg: str, repo_root: Path) -> Path:
    if not file_arg or "\\" in file_arg or ":" in file_arg or any(c in _GLOB for c in file_arg):
        raise _NotEvaluable("file operand must be a plain repo-relative path")
    pure = PurePosixPath(file_arg)
    if pure.is_absolute() or Path(file_arg).is_absolute() or ".." in pure.parts:
        raise _NotEvaluable("file operand must be repo-relative with no '..'")
    root = repo_root.resolve()
    target = (root / pure).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise _NotEvaluable("file operand resolves outside the repo root")
    return target


def _count(regexes: "List[re.Pattern[str]]", path: Path) -> Optional[int]:
    """None when grep would print no count (missing file or not a regular file)."""
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_BYTES:
        raise _NotEvaluable(f"file over {MAX_BYTES} bytes")
    text = path.read_bytes().decode("utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return sum(1 for line in lines if any(r.search(line) for r in regexes))


def _parse_expect(expect: str) -> List[Tuple[str, int]]:
    parts = [p.strip() for p in str(expect).strip().split(" then ")]
    out: List[Tuple[str, int]] = []
    for part in parts:
        op = "=="
        for cand in _OPS:
            if part.startswith(cand):
                op, part = cand, part[len(cand):].strip()
                break
        if not re.fullmatch(r"\d+", part):
            raise _NotEvaluable(f"expect {expect!r} is not an integer form")
        out.append((op, int(part)))
    return out


def _holds(op: str, want: int, got: int) -> bool:
    return {
        "==": got == want, ">=": got >= want, "<=": got <= want, ">": got > want, "<": got < want
    }[op]


def evaluate(command: str, expect: str, repo_root: Path) -> dict:
    """{"state": "MATCH" | "MISMATCH" | "NOT_EVALUABLE", "observed": str, "detail": str}.
    No spawn, no walk; reads only the files the command names, under repo_root.
    observed is the printed counts, one per line; a stage counting 0 (or a missing file) ends
    the chain as shell && does."""
    try:
        stages = _parse_stages(command)
        wants = _parse_expect(expect)
        plan = [
            ([_compile(m, p) for p in ps], _resolve(f, Path(repo_root))) for m, ps, f in stages
        ]
        counts: List[int] = []
        for regexes, path in plan:
            n = _count(regexes, path)
            if n is None:
                observed = "\n".join(str(c) for c in counts)
                return _result("MISMATCH", observed, f"no such file: {path.name}")
            counts.append(n)
            if n == 0:
                break
    except _NotEvaluable as exc:
        return _result("NOT_EVALUABLE", "", str(exc))
    observed = "\n".join(str(c) for c in counts)
    if len(wants) == len(counts) and all(_holds(o, w, c) for (o, w), c in zip(wants, counts)):
        return _result("MATCH", observed, "")
    return _result("MISMATCH", observed, f"expected {expect!r}")
