"""In-process evaluator for an anchor premise's check. No spawn, no walk: it streams only the
files the command names (any size), under the repo root it is handed. A stage that reads a git ref
(its own `git grep ... REF -- FILE`, or the caller's base_ref) counts over blobs the caller
fetched once with `read_at_ref`; the evaluator itself never spawns.

Grammar (shlex, posix): stage := [git] grep FLAGS (PATTERN | (-e PATTERN)+) [REF --] FILE, REF only
with `git`; FLAGS = c (or n, which implies c) plus at most one of E or F (a line counts when any -e
pattern matches); command := stage (("&&" | ";") stage)*; expect := N | >=N | <=N | >N | <N |
"N then M [then K ...]". Anything else is NOT_EVALUABLE."""

import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

GRAMMAR: str = (
    '[git] grep -c [-E|-F] (PATTERN | -e PATTERN ...) [REF --] FILE [&&|; grep ...]; '
    'expect N|>=N|<=N|>N|<N|"N then M"'
)

_GLOB = set("*?[]{}")
_BRE_UNESCAPE = set("|(){}")
_BRE_ESCAPE = set("(){}|+?")
_OPS = (">=", "<=", ">", "<")
# Python re has no timeout and files are streamed at any size, so catastrophic-backtracking
# shapes are refused before they can run: a quantified group holding a quantifier, and a
# quantified alternation group unless its branches are plain literals with pairwise distinct
# first characters.
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


Stage = Tuple[str, List[str], str, Optional[str]]


def _parse_stages(command: str) -> List[Stage]:
    toks = _tokens(command)
    if not toks:
        raise _NotEvaluable("empty command")
    stages: List[List[str]] = [[]]
    for tok in toks:
        if tok in ("&&", ";"):
            stages.append([])
        elif re.fullmatch(r"[&|;<>()]+", tok):
            raise _NotEvaluable(f"unsupported shell operator {tok!r}")
        else:
            stages[-1].append(tok)
    return [_parse_stage(s) for s in stages]


def _valid_ref(ref: str) -> bool:
    """A ref token git reads as one revision name: no whitespace or control byte, no `..`, no
    leading `-`, no `:` (which would turn it into a `<rev>:<path>` blob spec)."""
    if not ref or not ref.isprintable() or any(c.isspace() for c in ref):
        return False
    return ".." not in ref and not ref.startswith("-") and ":" not in ref


def _parse_stage(words: List[str]) -> Stage:
    """(mode, patterns, path, ref); ref is None for a plain grep stage."""
    git = words[:2] == ["git", "grep"]
    if not words or not (git or words[0] == "grep"):
        raise _NotEvaluable("only grep stages are evaluable")
    flags: List[str] = []
    patterns: List[str] = []
    rest = words[2 if git else 1:]
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag == "-e":
            if not rest:
                raise _NotEvaluable("-e needs a pattern")
            patterns.append(rest.pop(0))
            continue
        body = flag[1:]
        if not body or not set(body) <= set("cEFn"):
            raise _NotEvaluable(f"unsupported grep flag {flag!r}")
        flags.extend(body)
    if flags.count("n") > 1:
        raise _NotEvaluable("flags must include c once, with at most one of E or F")
    if "n" in flags:
        # Line numbers change no match: -n alone counts the printed lines, -cn is -c.
        flags = [f for f in flags if f != "n"]
        if "c" not in flags:
            flags.append("c")
    if len(flags) != len(set(flags)) or "c" not in flags:
        raise _NotEvaluable("flags must include c once, with at most one of E or F")
    if "E" in flags and "F" in flags:
        raise _NotEvaluable("E and F are mutually exclusive")
    ref: Optional[str] = None
    if git:
        if len(rest) < 3 or rest[-2] != "--":
            raise _NotEvaluable("a git grep stage ends with `<ref> -- <path>`")
        ref = rest[-3]
        if not _valid_ref(ref):
            raise _NotEvaluable(f"ref {ref!r} is not a plain revision name")
        rest = rest[:-3] + rest[-1:]
    if patterns:
        if len(rest) != 1:
            raise _NotEvaluable("a stage with -e patterns takes exactly one file")
    else:
        if len(rest) != 2:
            raise _NotEvaluable("a stage takes exactly one pattern and one file")
        patterns.append(rest.pop(0))
    mode = "F" if "F" in flags else "E" if "E" in flags else "B"
    return mode, patterns, rest[0], ref


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
    # `~` is home, not a repo dir: joined under the root it reads as a missing file, i.e. a false
    # MISMATCH -> CONTRADICTED block. Out-of-repo paths are NOT_EVALUABLE, like absolute ones.
    if pure.parts[0].startswith("~") or pure.is_absolute() or Path(file_arg).is_absolute() or ".." in pure.parts:
        raise _NotEvaluable("file operand must be repo-relative with no '..'")
    root = repo_root.resolve()
    target = (root / pure).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise _NotEvaluable("file operand resolves outside the repo root")
    return target


def _count_lines(regexes: "List[re.Pattern[str]]", raws: Iterable[bytes]) -> int:
    n = 0
    for raw in raws:
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        line = raw.decode("utf-8", errors="replace")
        if any(r.search(line) for r in regexes):
            n += 1
    return n


def _count(regexes: "List[re.Pattern[str]]", path: Path) -> Optional[int]:
    """None when grep would print no count (missing file or not a regular file)."""
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        return _count_lines(regexes, fh)


def _blob_lines(data: bytes) -> Iterator[bytes]:
    parts = data.split(b"\n")
    for i, part in enumerate(parts):
        if i < len(parts) - 1:
            yield part + b"\n"
        elif part:
            yield part


def _count_bytes(regexes: "List[re.Pattern[str]]", data: bytes) -> int:
    """`_count` over blob bytes: the same CRLF/LF line rule as a file read."""
    return _count_lines(regexes, _blob_lines(data))


def _rel(file_arg: str) -> str:
    return PurePosixPath(file_arg).as_posix()


def blob_pairs(command: str, base_ref: Optional[str] = None) -> List[Tuple[str, str]]:
    """The (ref, path) pairs `evaluate` will look up in `blobs` for this command: each stage's own
    ref, else base_ref; tree-read stages contribute none. Unparsable command -> []."""
    try:
        stages = _parse_stages(command)
    except _NotEvaluable:
        return []
    return [(ref or base_ref, _rel(f)) for _, _, f, ref in stages if (ref or base_ref)]


def read_at_ref(
    repo_root: Path, pairs: Iterable[Tuple[str, str]]
) -> Dict[Tuple[str, str], Optional[bytes]]:
    """{(ref, path): blob bytes | None} from ONE `git cat-file --batch` spawn; None is a path git
    finds no blob for at that ref. A ref git cannot resolve, or one failing `_valid_ref`, has NO
    key (the caller reads that as unreadable). Zero pairs -> zero spawns."""
    from coordinator_core.git.run import run_git

    wanted = [
        (r, p) for r, p in dict.fromkeys(pairs) if _valid_ref(r) and p and "\n" not in p
    ]
    refs = list(dict.fromkeys(r for r, _ in wanted))
    if not refs:
        return {}
    specs = [f"{r}^{{commit}}" for r in refs] + [f"{r}:{p}" for r, p in wanted]
    res = run_git(
        ["-C", str(repo_root), "cat-file", "--batch"],
        input=("\n".join(specs) + "\n").encode("utf-8"),
    )
    if res.returncode != 0:
        return {}
    data, pos, replies = res.stdout_bytes, 0, []
    for _ in specs:
        end = data.find(b"\n", pos)
        if end < 0:
            return {}
        header = data[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[2].isdigit():
            size = int(header[2])
            replies.append((header[1], data[pos:pos + size]))
            pos += size + 1
        else:
            replies.append((None, b""))
    resolvable = {r for r, (kind, _) in zip(refs, replies) if kind == b"commit"}
    return {
        (r, p): (body if kind == b"blob" else None)
        for (r, p), (kind, body) in zip(wanted, replies[len(refs):])
        if r in resolvable
    }


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


def evaluable(command: str) -> Optional[str]:
    """None when the command parses as GRAMMAR stages, else the refusal detail. Parse only:
    reads no file, takes no repo root. Stage shape only: pattern compilation and expect are not
    checked. No caller outside tests yet."""
    try:
        _parse_stages(command)
    except _NotEvaluable as exc:
        return str(exc)
    return None


def evaluate(
    command: str,
    expect: str,
    repo_root: Path,
    base_ref: Optional[str] = None,
    blobs: Optional[Mapping[Tuple[str, str], Optional[bytes]]] = None,
) -> dict:
    """{"state": "MATCH" | "MISMATCH" | "NOT_EVALUABLE", "observed": str, "detail": str}.
    No spawn, no walk; reads only the files the command names, under repo_root.
    observed is the printed counts, one per line. Every stage runs and reports its count, 0
    included (grep's exit 1 on zero matches is a result, not a failure); a missing file ends
    the chain.
    A stage's effective ref is its own `git grep` ref, else base_ref, else none (the tree). A
    ref-read stage counts over blobs[(ref, path)] (from `read_at_ref`): None -> MISMATCH "no such
    file at <ref>"; a key absent from blobs -> NOT_EVALUABLE "base ref <ref> unreadable"."""
    try:
        stages = _parse_stages(command)
        wants = _parse_expect(expect)
        plan = [
            ([_compile(m, p) for p in ps], _resolve(f, Path(repo_root)), ref or base_ref, _rel(f))
            for m, ps, f, ref in stages
        ]
        counts: List[int] = []
        for regexes, path, ref, rel in plan:
            if ref is None:
                n = _count(regexes, path)
                gone = f"no such file: {path.name}"
            else:
                if not _valid_ref(ref) or blobs is None or (ref, rel) not in blobs:
                    raise _NotEvaluable(f"base ref {ref} unreadable")
                blob = blobs[(ref, rel)]
                n = None if blob is None else _count_bytes(regexes, blob)
                gone = f"no such file at {ref}: {path.name}"
            if n is None:
                observed = "\n".join(str(c) for c in counts)
                return _result("MISMATCH", observed, gone)
            counts.append(n)
    except _NotEvaluable as exc:
        return _result("NOT_EVALUABLE", "", str(exc))
    observed = "\n".join(str(c) for c in counts)
    if len(wants) == len(counts) and all(_holds(o, w, c) for (o, w), c in zip(wants, counts)):
        return _result("MATCH", observed, "")
    detail = f"expected {expect!r}"
    if len(wants) == len(counts):
        for (o, w), c, (_, path, _, _) in zip(wants, counts, plan):
            if not _holds(o, w, c):
                detail += f"; {path.name} counted {c}"
                break
    return _result("MISMATCH", observed, detail)
