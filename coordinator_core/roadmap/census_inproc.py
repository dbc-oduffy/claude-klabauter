"""coordinator_core.roadmap.census_inproc -- answer an order-insensitive read-only census
pipeline without a spawn.

`evaluate_many(commands, files, store, read_budget_s)` returns, per command, the
`(returncode, stdout, stderr)` the screened POSIX pipeline would produce over the git index world
(`IndexFiles`: tracked paths and blobs), or None when it cannot promise that exactly (the caller
spawns), or PENDING when the budget ran out first. A decline is always safe; a wrong answer never
is, so every doubt declines. Answers are composed from per-(pattern, blob oid) match records kept
in the `CensusStore`, one blob batch per slice, so a warm pass reads only blobs it has no record
for.

GRAMMAR (anything else declines). Examples, one per line, are run against the host shell by
the parity test:

    grep [-r] [-l|-c|-n|-h|-i|-w|-v|-F|-E|-o]... [--include=G|--exclude=G|--exclude-dir=G]... PAT OPERAND...
        multi-file output only when the pipeline ends in `| wc -l`; single file any mode
    [above] | grep [-v] [-F|-E] PAT ... | wc -l
    wc -l FILE        find OPERAND [-name G] [-type f|d] | wc -l        ls OPERAND | wc -l

    $ grep -c needle src/top.py
    $ grep -n needle src/top.py
    $ grep -o needle src/top.py
    $ grep -l needle src/top.py
    $ grep -c -v needle src/top.py
    $ grep -ci NEEDLE src/top.py
    $ grep -c needle docs/noeol.txt
    $ grep -c 'nee.le$' docs/crlf.md
    $ wc -l src/top.py
    $ grep -rl needle src | wc -l
    $ grep -rl --include=*.py needle src | wc -l
    $ grep -rl --exclude=*.py needle src | wc -l
    $ grep -rl --exclude-dir=vendor needle lib | wc -l
    $ grep -rc needle src | wc -l
    $ grep -rn needle src/pkg | wc -l
    $ grep -ro needle src/pkg | wc -l
    $ grep -rlE 'need(le|les)' src | wc -l
    $ grep -rlF needle src docs | wc -l
    $ grep -rli NEEDLE src | wc -l
    $ grep -rlw needle src/pkg | wc -l
    $ grep -rlv needle src/pkg | wc -l
    $ grep -rl 'need\\|nothing' src 2>/dev/null | wc -l
    $ grep -rl needle src nowhere 2>/dev/null | wc -l
    $ grep -rl needle src | grep -v pycache | wc -l
    $ grep -rc needle src | grep -v ':0' | wc -l
    $ find src -name '*.py' | wc -l
    $ find src -type d | wc -l
    $ ls src | wc -l

Output form of `wc -l` is f"{n}\\n" (GNU); callers compare after strip.

DECLINES: a stage outside GRAMMAR; a shell construct this module does not read (glob, variable,
tilde, `&&`, `;`, a redirect other than `2>/dev/null`); a regex outside the closed translator
(back-references, POSIX classes beyond alpha/digit/space/alnum, `\\<`, `\\>`, BRE `\\+`/`\\?`,
a leading `*`, an `^` or `$` the dialects read differently); `-o` over a quantifier or an
alternation (leftmost-longest differs from Python's leftmost-first); a regex that depends on how
bytes group into characters met in a file holding a non-ASCII byte; a binary or non-UTF-8 file
matched under a content-printing mode; an absent operand without `2>/dev/null`; an unreadable
or unbounded operand; `-R`; a root operand; a symlink operand.

BINARY: a NUL in a file's first 32KiB makes it binary. It counts under `-l`, `-c` and the
`| wc -l` count; its count is trusted only when splitting lines on LF alone and on LF-or-NUL
agree, because GNU grep treats NUL as a line end in a binary file and BSD grep does not.

NOT UTF-8: a file with no NUL and invalid UTF-8 is undecidable. BSD grep skips its lines in a
UTF-8 locale and GNU grep matches them, so a walk that meets one declines.

STORE: per blob oid, per pattern key, `(matched, count, kind)` packed into one int. `matched` is
None when the blob's answer is undecidable (a decline); `count` is -1 when only `matched` is
trusted; `kind` is 0 text, 1 not UTF-8, 2 holds NUL. No line text is ever stored.

FILES: `IndexFiles.files(rel)` lists the tracked `(path, oid)` at or under `rel`; `find` and `ls`
answer from those paths alone.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Optional, Protocol

from coordinator_core.roadmap import census

GENERATES = []

Ran = tuple  # (returncode, stdout, stderr), the shape `_spawn` returns

_BINARY_PROBE = 32 * 1024
_GREP_FLAGS = frozenset("rlcnhiwvFEo")
_STAGE_FLAGS = frozenset("vFE")
_DYNAMIC_CHARS = frozenset("*?[]{}~$\\!#`")
_GLOB_FLAGS = ("--include=", "--exclude=", "--exclude-dir=")
_POSIX_CLASSES = {
    "alpha": "a-zA-Z",
    "digit": "0-9",
    "space": " \\t\\n\\r\\f\\v",
    "alnum": "a-zA-Z0-9",
}
_REPEAT_LIMIT = 1000


class _Decline(Exception):
    """Internal: this command cannot be answered exactly in-process."""


@dataclass
class _Rx:
    """A translated regex: Python source plus what the translator learned about it."""

    src: str
    sensitive: bool = False  # depends on how bytes group into characters
    newline_safe: bool = True  # no construct can match a line feed
    quantified: bool = False
    alternated: bool = False


def _bracket(pat: str, i: int) -> tuple:
    """(python class source, next index, negated) for the bracket expression whose `[` is at
    `i - 1`, or raise _Decline."""
    n = len(pat)
    neg = i < n and pat[i] == "^"
    if neg:
        i += 1
    items: list[str] = []
    first = True
    while True:
        if i >= n:
            raise _Decline
        c = pat[i]
        if c == "]" and not first:
            i += 1
            break
        first = False
        if c == "[" and i + 1 < n and pat[i + 1] in ":.=":
            if pat[i + 1] != ":":
                raise _Decline
            end = pat.find(":]", i + 2)
            if end < 0 or pat[i + 2:end] not in _POSIX_CLASSES:
                raise _Decline
            items.append(_POSIX_CLASSES[pat[i + 2:end]])
            i = end + 2
            continue
        if i + 2 < n and pat[i + 1] == "-" and pat[i + 2] != "]":
            lo, hi = c, pat[i + 2]
            if hi == "[" or not (lo.isalnum() and hi.isalnum() and lo <= hi):
                raise _Decline
            items.append(f"{lo}-{hi}")
            i += 3
            continue
        if c == "-" and not (not items or (i + 1 < n and pat[i + 1] == "]")):
            raise _Decline
        items.append("\\" + c if c in "\\]^[-" else c)
        i += 1
    if not items:
        raise _Decline
    return "[" + ("^" if neg else "") + "".join(items) + "]", i, neg


def _interval(pat: str, i: int, close: str) -> tuple:
    """(`{m,n}`, next index) for an interval body starting at `i`, ended by `close`."""
    end = pat.find(close, i)
    if end < 0:
        raise _Decline
    m = re.fullmatch(r"(\d+)(,(\d*))?", pat[i:end])
    if not m:
        raise _Decline
    lo = int(m.group(1))
    hi = m.group(3)
    if lo > _REPEAT_LIMIT or (hi and int(hi) > _REPEAT_LIMIT) or (hi and int(hi) < lo):
        raise _Decline
    body = f"{lo}" if m.group(2) is None else f"{lo},{hi or ''}"
    return "{" + body + "}", end + len(close)


def _translate(pat: str, dialect: str) -> Optional[_Rx]:
    """Python source for a BRE (`B`), ERE (`E`) or fixed-string (`F`) pattern, or None when the
    pattern leaves the closed subset."""
    if not pat or not pat.isascii() or "\n" in pat or "\0" in pat:
        return None
    if dialect == "F":
        return _Rx(re.escape(pat))
    try:
        return _translate_regex(pat, dialect == "E")
    except _Decline:
        return None


def _translate_regex(pat: str, ere: bool) -> _Rx:
    rx = _Rx("")
    out: list[str] = []
    n = len(pat)
    i = 0
    prev = "start"  # start | atom | group | quant | anchor
    group_quant: list[bool] = []
    closed_group_quant = False

    def quantify(q: str) -> None:
        nonlocal prev
        if prev not in ("atom", "group") or (prev == "group" and closed_group_quant):
            raise _Decline
        out.append(q)
        prev = "quant"
        rx.quantified = True
        if group_quant:
            group_quant[-1] = True

    def open_group() -> None:
        nonlocal prev
        out.append("(?:")
        group_quant.append(False)
        prev = "start"

    def close_group() -> None:
        nonlocal prev, closed_group_quant
        if not group_quant or prev == "start":
            raise _Decline
        out.append(")")
        closed_group_quant = group_quant.pop()
        if closed_group_quant and group_quant:
            group_quant[-1] = True
        prev = "group"

    def alternate() -> None:
        nonlocal prev
        if prev == "start":
            raise _Decline
        out.append("|")
        rx.alternated = True
        prev = "start"

    while i < n:
        c = pat[i]
        if c == "\\":
            if i + 1 >= n:
                raise _Decline
            d = pat[i + 1]
            i += 2
            if not ere and d == "(":
                open_group()
            elif not ere and d == ")":
                close_group()
            elif not ere and d == "|":
                alternate()
            elif not ere and d == "{":
                body, i = _interval(pat, i, "\\}")
                quantify(body)
            elif d.isdigit() or d in "<>B`'" or (not ere and d in "+?"):
                raise _Decline
            elif d == "b":
                rx.sensitive = True
                out.append("\\b")
                prev = "anchor"
            elif d in "wWsS":
                rx.sensitive = True
                if d != "w":
                    rx.newline_safe = False
                out.append("\\" + d)
                prev = "atom"
            elif d.isalnum():
                raise _Decline
            else:
                out.append(re.escape(d))
                prev = "atom"
        elif c == ".":
            rx.sensitive = True
            out.append(".")
            prev = "atom"
            i += 1
        elif c == "[":
            src, i, neg = _bracket(pat, i + 1)
            if neg:
                rx.sensitive = True
                rx.newline_safe = False
            if "\\n" in src or "\\r\\f" in src:
                rx.newline_safe = False
            out.append(src)
            prev = "atom"
        elif c == "^":
            if prev != "start":
                raise _Decline
            out.append("^")
            prev = "anchor"
            i += 1
        elif c == "$":
            tail = pat[i + 1:]
            if tail and not (tail.startswith((")", "|")) if ere else tail.startswith(("\\)", "\\|"))):
                raise _Decline
            out.append("$")
            prev = "anchor"
            i += 1
        elif c == "*":
            quantify("*")
            i += 1
        elif ere and c in "+?":
            quantify(c)
            i += 1
        elif ere and c == "{":
            body, i = _interval(pat, i + 1, "}")
            quantify(body)
        elif ere and c == "(":
            open_group()
            i += 1
        elif ere and c == ")":
            close_group()
            i += 1
        elif ere and c == "|":
            alternate()
            i += 1
        else:
            out.append(re.escape(c))
            prev = "atom"
            i += 1
    if group_quant or prev == "start" and out:
        raise _Decline
    rx.src = "".join(out)
    return rx


@dataclass
class _Matcher:
    """A compiled pattern with the flags that change a per-file answer."""

    rx: "re.Pattern"
    sensitive: bool
    newline_safe: bool
    invert: bool
    only: bool
    key: str

    def scan(self, data: bytes) -> tuple:
        """`(matched, count, kind)` for one file's bytes."""
        kind = _kind(data)
        nul = kind == 2
        if kind == 1 or (self.sensitive and not data.isascii()):
            return (None, -1, kind)
        if self.newline_safe and not self.invert and self.rx.search(data) is None:
            return (False, 0, kind)
        first = self._count(data.split(b"\n"), data, (b"\n",))
        if nul:
            second = self._count(re.split(b"[\n\0]", data), data, (b"\n", b"\0"))
            if first[0] != second[0]:
                return (None, -1, kind)
            if first[1] != second[1]:
                return (first[0], -1, kind)
        return (first[0], first[1], kind)

    def _count(self, lines: list, data: bytes, ends: tuple) -> tuple:
        if lines and lines[-1] == b"" and (not data or data.endswith(ends)):
            lines = lines[:-1]
        selected = 0
        occurrences = 0
        search = self.rx.search
        for ln in lines:
            if (search(ln) is not None) != self.invert:
                selected += 1
                if self.only:
                    occurrences += sum(1 for m in self.rx.finditer(ln) if m.end() > m.start())
        return (selected > 0, occurrences if self.only else selected)


def _kind(data: bytes) -> int:
    """0 text, 1 not UTF-8, 2 holds a NUL in its first 32KiB."""
    if b"\0" in data[:_BINARY_PROBE]:
        return 2
    if data.isascii():
        return 0
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return 1
    return 0


_WORD = "[A-Za-z0-9_]"


def _matcher(pat: str, dialect: str, *, ignore: bool, word: bool, invert: bool,
             only: bool) -> Optional[_Matcher]:
    rx = _translate(pat, dialect)
    if rx is None:
        return None
    if only and (rx.quantified or rx.alternated or invert):
        return None
    src = rx.src
    sensitive = rx.sensitive
    if word:
        src = f"(?<!{_WORD})(?:{src})(?!{_WORD})"
        sensitive = True
    flags = re.IGNORECASE if ignore else 0
    try:
        compiled = re.compile(src.encode("ascii"), flags | re.MULTILINE)
    except (re.error, RecursionError, OverflowError):
        return None
    key = hashlib.sha1(
        repr((src, ignore, word, invert, only)).encode("ascii")
    ).hexdigest()[:16]
    return _Matcher(compiled, sensitive, rx.newline_safe, invert, only, key)


@dataclass
class _Grep:
    flags: set = field(default_factory=set)
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    exclude_dir: list = field(default_factory=list)
    pattern: str = ""
    operands: list = field(default_factory=list)
    quiet: bool = False

    @property
    def dialect(self) -> str:
        return "F" if "F" in self.flags else "E" if "E" in self.flags else "B"


def _static(tok: str, raw: str) -> bool:
    """True when the shell would pass `tok` through untouched: no expansion character, or the
    token sits verbatim inside one pair of quotes in the command text."""
    if not any(c in _DYNAMIC_CHARS for c in tok):
        return True
    if "'" not in tok and f"'{tok}'" in raw:
        return True
    return '"' not in tok and "$" not in tok and "`" not in tok and "!" not in tok \
        and f'"{tok}"' in raw


def _split_quiet(parts: list) -> tuple:
    """(tokens without `2>/dev/null` redirects, how many were removed)."""
    out: list[str] = []
    quiet = 0
    i = 0
    while i < len(parts):
        if parts[i:i + 3] == ["2", ">", "/dev/null"]:
            quiet += 1
            i += 3
            continue
        out.append(parts[i])
        i += 1
    if any(t in ("<", ">", ">>", ">&", "&>", "&>>") for t in out):
        raise _Decline
    return out, quiet


def _parse_grep(parts: list, raw: str, root: Optional[Path], *, stage_only: bool) -> _Grep:
    g = _Grep()
    positional: list[str] = []
    rest = False
    for tok in parts[1:]:
        if rest or not tok.startswith("-") or tok == "-":
            if not _static(tok, raw):
                raise _Decline
            positional.append(tok)
        elif tok == "--":
            rest = True
        elif tok.startswith("--"):
            flag = next((f for f in _GLOB_FLAGS if tok.startswith(f)), None)
            glob = tok[len(flag):] if flag else ""
            if (stage_only or flag is None or not glob or "/" in glob
                    or any(c in glob for c in "{}~$\\!#`")):
                raise _Decline
            {"--include=": g.include, "--exclude=": g.exclude,
             "--exclude-dir=": g.exclude_dir}[flag].append(glob)
        else:
            chars = tok[1:]
            allowed = _STAGE_FLAGS if stage_only else _GREP_FLAGS
            if not chars or any(c not in allowed for c in chars):
                raise _Decline
            g.flags.update(chars)
    if root is not None and (g.include or g.exclude or g.exclude_dir) \
            and any(e.startswith("--") for e in os.listdir(root)):
        raise _Decline
    if not positional:
        raise _Decline
    g.pattern, g.operands = positional[0], positional[1:]
    if ("F" in g.flags and "E" in g.flags) or ("l" in g.flags and "c" in g.flags) \
            or ("o" in g.flags and ("c" in g.flags or "l" in g.flags)):
        raise _Decline
    if g.include and g.exclude:
        raise _Decline
    return g


@dataclass
class _Operand:
    token: str
    rel: str
    kind: str  # file | dir | absent
    clean: bool  # printed paths equal what the shell prints
    entries: list = field(default_factory=list)  # index entries (path, oid, mode) at/under rel


def _norm_operand(token: str, raw: str) -> tuple:
    """`(rel, clean)` for a static relative operand token, or raise _Decline."""
    if not _static(token, raw) or token.startswith(("/", "~", "-")) or re.match(r"^[A-Za-z]:", token):
        raise _Decline
    rel = token
    while rel.startswith("./"):
        rel = rel[2:]
    clean = rel == token and not rel.endswith("/")
    rel = rel.rstrip("/")
    parts = rel.split("/")
    if not rel or rel == "." or ".." in parts or "" in parts or "." in parts:
        raise _Decline
    return rel, clean


def _mode(g: _Grep) -> str:
    return "l" if "l" in g.flags else "c" if "c" in g.flags else "print"


def _print_single(g: _Grep, m: _Matcher, data: Optional[bytes]) -> Ran:
    """A content-printing single-file grep over one blob's bytes."""
    if data is None or b"\0" in data[:_BINARY_PROBE]:
        raise _Decline
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Decline
    if m.sensitive and not data.isascii():
        raise _Decline
    lines = text.split("\n")
    if lines and lines[-1] == "" and (text.endswith("\n") or not text):
        lines.pop()
    out: list[str] = []
    selected = 0
    numbered = "n" in g.flags
    for no, ln in enumerate(lines, 1):
        hit = m.rx.search(ln.encode("utf-8")) is not None
        if hit == m.invert:
            continue
        selected += 1
        if m.only:
            for found in m.rx.finditer(ln.encode("utf-8")):
                if found.end() > found.start():
                    piece = found.group().decode("utf-8")
                    out.append(f"{no}:{piece}" if numbered else piece)
        else:
            out.append(f"{no}:{ln}" if numbered else ln)
    stdout = "".join(f"{x}\n" for x in out).replace("\r\n", "\n").replace("\r", "\n")
    return (0 if selected else 1, stdout, "")


def _filter_stage(parts: list, raw: str, lines: list, root: Path) -> list:
    g = _parse_grep(parts, raw, root, stage_only=True)
    if g.operands:
        raise _Decline
    m = _matcher(g.pattern, g.dialect, ignore=False, word=False, invert=False, only=False)
    if m is None:
        raise _Decline
    drop = "v" in g.flags
    kept: list[bytes] = []
    for ln in lines:
        if m.sensitive and not ln.isascii():
            raise _Decline
        if (m.rx.search(ln) is not None) != drop:
            kept.append(ln)
    return kept


def _find_spec(parts: list, raw: str, make_operand) -> tuple:
    """`(operands, keep)` for `find OPERAND... [-name G] [-type f|d]`; `keep(kind, name)` is the
    predicate chain."""
    i = 1
    operands: list[_Operand] = []
    while i < len(parts) and not parts[i].startswith("-"):
        operands.append(make_operand(parts[i]))
        i += 1
    names: list[str] = []
    types: list[str] = []
    while i < len(parts):
        if i + 1 >= len(parts) or not _static(parts[i + 1], raw):
            raise _Decline
        if parts[i] == "-name" and "/" not in parts[i + 1]:
            names.append(parts[i + 1])
        elif parts[i] == "-type" and parts[i + 1] in ("f", "d"):
            types.append(parts[i + 1])
        else:
            raise _Decline
        i += 2
    if not operands or not all(op.clean for op in operands):
        raise _Decline

    def keep(kind: str, name: str) -> bool:
        return all(t == kind for t in types) and all(fnmatch.fnmatchcase(name, g) for g in names)

    return operands, keep


def _ls_arg(parts: list) -> str:
    args = [t for t in parts[1:] if t != "-1"]
    if len(args) != 1 or args[0].startswith("-"):
        raise _Decline
    return args[0]


def _split_command(command: str) -> tuple:
    """`(stages without 2>/dev/null, per-stage quiet flags)` of a screened single pipeline, or
    raise _Decline."""
    if census.screen(command) is not None:
        raise _Decline
    if any(t in command for t in ("&&", "||", ";", "\n", "\r")):
        raise _Decline
    stages = census._split_stages(command)
    if not stages or any(not st for st in stages):
        raise _Decline
    cleaned = []
    removed = []
    for st in stages:
        tokens, n_removed = _split_quiet(st)
        cleaned.append(tokens)
        removed.append(n_removed)
    if command.count(">") != sum(removed) or command.count("2>/dev/null") != sum(removed):
        raise _Decline
    if any(not st for st in cleaned):
        raise _Decline
    return cleaned, [n > 0 for n in removed]


_SLICE = 256
_WC_KEY = "wc-l"
_SYMLINK = "120000"
_GITLINK = "160000"


class _Pending:
    """Type of PENDING."""

    def __repr__(self) -> str:
        return "PENDING"


PENDING = _Pending()


class IndexFiles(Protocol):
    """The index world `evaluate_many` reads: tracked paths and their blobs, never the worktree."""

    def files(self, rel: str) -> list:
        """Tracked `(path, oid)` at or under `rel`, sorted; a third element, the git mode string,
        is read when present (`120000` symlink is skipped under -r and declined as an operand)."""

    def read_blobs(self, oids: Collection[str]) -> dict:
        """`{oid: bytes}` for every oid asked, in one call."""


class MatchStore(Protocol):
    def matched(self, pattern_key: str, oid: str) -> Optional[int]: ...

    def put_matched(self, pattern_key: str, counts: dict) -> None: ...


class _Newlines:
    """Matcher stand-in recording a blob's newline count under the reserved `wc-l` key."""

    key = _WC_KEY

    def scan(self, data: bytes) -> tuple:
        n = data.count(b"\n")
        return (n > 0, n, 0)


_NEWLINES = _Newlines()


def _pack(res: tuple) -> int:
    """`(matched, count, kind)` as one non-negative int: count+1 in the high digits, kind (0-2)
    and matched (0 undecidable, 1 no, 2 yes) in the low twelve."""
    matched, count, kind = res
    return (count + 1) * 12 + kind * 4 + (0 if matched is None else 2 if matched else 1)


def _unpack(v: int) -> tuple:
    return ([None, False, True][v % 4], v // 12 - 1, (v // 4) % 3)


def _valid(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0 and v % 4 != 3 \
        and (v // 4) % 3 != 3


def _cpu_s() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


@dataclass
class _Plan:
    needs: list  # (matcher, oid) records the answer is composed from
    raw: list  # oids whose bytes the answer prints from
    finish: Callable  # (get, rawget) -> Ran; raises _Decline


def _entries(files: IndexFiles, rel: str) -> list:
    out = []
    for e in files.files(rel):
        path, oid = e[0], e[1]
        mode = str(e[2]) if len(e) > 2 else ""
        if mode == _GITLINK:
            continue
        if path == rel or path.startswith(rel + "/"):
            out.append((path, oid, mode))
    return out


def _operand_ix(token: str, files: IndexFiles, raw: str) -> _Operand:
    rel, clean = _norm_operand(token, raw)
    entries = _entries(files, rel)
    if not entries:
        return _Operand(token, rel, "absent", clean)
    if any(p == rel for p, _, _ in entries):
        return _Operand(token, rel, "file", clean, [e for e in entries if e[0] == rel])
    return _Operand(token, rel, "dir", clean, entries)


def _selected_ix(g: _Grep, ops: list) -> list:
    """`(rel, oid)` of every tracked file the grep reads, with its filters applied."""
    chosen: list = []
    for op in ops:
        if op.kind == "absent":
            if not g.quiet:
                raise _Decline
            continue
        if op.kind == "file":
            if g.include or g.exclude or op.entries[0][2] == _SYMLINK:
                raise _Decline
            chosen.append((op.rel, op.entries[0][1]))
            continue
        if "r" not in g.flags:
            raise _Decline
        base = op.rel.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatchcase(base, d) for d in g.exclude_dir):
            raise _Decline
        prefix = len(op.rel) + 1
        filtered = bool(g.exclude_dir or g.include or g.exclude)
        for rel, oid, mode in op.entries:
            if mode == _SYMLINK:
                continue
            if not filtered:
                chosen.append((rel, oid))
                continue
            name = rel.rsplit("/", 1)[-1]
            if any(fnmatch.fnmatchcase(d, glob) for d in rel[prefix:].split("/")[:-1]
                   for glob in g.exclude_dir):
                continue
            if g.include and not any(fnmatch.fnmatchcase(name, p) for p in g.include):
                continue
            if any(fnmatch.fnmatchcase(name, p) for p in g.exclude):
                continue
            chosen.append((rel, oid))
    return chosen


def _plan_grep_pipeline(g: _Grep, ops: list, cleaned: list, command: str) -> _Plan:
    m = _matcher(g.pattern, g.dialect, ignore="i" in g.flags, word="w" in g.flags,
                 invert="v" in g.flags, only="o" in g.flags)
    if m is None:
        raise _Decline
    mode = _mode(g)
    chosen = _selected_ix(g, ops)
    names_needed = len(cleaned) > 2
    if names_needed and (not all(op.clean for op in ops) or (
            "r" in g.flags and any(op.kind == "file" for op in ops))):
        raise _Decline
    prefixed = "h" not in g.flags and (
        "r" in g.flags and any(op.kind == "dir" for op in ops) or len(ops) > 1
    )
    for parts in cleaned[1:-1]:
        _filter_stage(parts, command, [], None)

    def finish(get, rawget) -> Ran:
        lines: list[bytes] = []
        for rel, oid in chosen:
            matched, count, kind = get(m.key, oid)
            if matched is None:
                raise _Decline
            if mode == "l":
                if matched:
                    lines.append(os.fsencode(rel))
            elif mode == "c":
                if count < 0:
                    raise _Decline
                text = f"{rel}:{count}" if prefixed else str(count)
                lines.append(os.fsencode(text))
            else:
                if names_needed:
                    raise _Decline
                if count < 0 or (matched and kind != 0):
                    raise _Decline
                lines.extend(b"" for _ in range(count))
        for parts in cleaned[1:-1]:
            lines = _filter_stage(parts, command, lines, None)
        return (0, f"{len(lines)}\n", "")

    return _Plan([(m, oid) for _, oid in chosen], [], finish)


def _plan_single(g: _Grep, op: _Operand) -> _Plan:
    if op.kind != "file" or "r" in g.flags or g.include or g.exclude or g.exclude_dir:
        raise _Decline
    _, oid, emode = op.entries[0]
    if emode == _SYMLINK:
        raise _Decline
    m = _matcher(g.pattern, g.dialect, ignore="i" in g.flags, word="w" in g.flags,
                 invert="v" in g.flags, only="o" in g.flags)
    if m is None:
        raise _Decline
    mode = _mode(g)
    if mode == "print":
        return _Plan([], [oid], lambda get, rawget: _print_single(g, m, rawget(oid)))

    def finish(get, rawget) -> Ran:
        matched, count, _ = get(m.key, oid)
        if matched is None or (mode == "c" and count < 0):
            raise _Decline
        if mode == "l":
            return (0 if matched else 1, f"{op.token}\n" if matched else "", "")
        return (0 if count > 0 else 1, f"{count}\n", "")

    return _Plan([(m, oid)], [], finish)


def _plan_wc(parts: list, raw: str, files: IndexFiles) -> _Plan:
    if len(parts) != 3 or parts[1] != "-l":
        raise _Decline
    op = _operand_ix(parts[2], files, raw)
    if op.kind != "file" or op.entries[0][2] == _SYMLINK:
        raise _Decline
    oid = op.entries[0][1]

    def finish(get, rawget) -> Ran:
        return (0, f"{get(_WC_KEY, oid)[1]} {op.token}\n", "")

    return _Plan([(_NEWLINES, oid)], [], finish)


def _find_names(parts: list, raw: str, files: IndexFiles, quiet: bool) -> list:
    operands, keep = _find_spec(parts, raw, lambda t: _operand_ix(t, files, raw))
    out: list[bytes] = []
    for op in operands:
        if op.kind == "absent":
            if not quiet:
                raise _Decline
            continue
        if op.kind == "file" and op.entries[0][2] == _SYMLINK:
            raise _Decline
        if keep("f" if op.kind == "file" else "d", op.rel.rsplit("/", 1)[-1]):
            out.append(os.fsencode(op.token))
        if op.kind != "dir":
            continue
        dirs = set()
        for path, _, mode in op.entries:
            comps = path[len(op.rel) + 1:].split("/")
            for k in range(1, len(comps)):
                dirs.add(tuple(comps[:k]))
            if keep("l" if mode == _SYMLINK else "f", comps[-1]):
                out.append(os.fsencode(op.token + "/" + "/".join(comps)))
        out.extend(os.fsencode(op.token + "/" + "/".join(d)) for d in sorted(dirs)
                   if keep("d", d[-1]))
    return out


def _ls_names(parts: list, raw: str, files: IndexFiles, quiet: bool) -> list:
    op = _operand_ix(_ls_arg(parts), files, raw)
    if op.kind == "absent":
        if not quiet:
            raise _Decline
        return []
    if op.kind == "file":
        return [os.fsencode(op.token)]
    names = {p[len(op.rel) + 1:].split("/", 1)[0] for p, _, _ in op.entries}
    return [os.fsencode(n) for n in sorted(names) if not n.startswith(".")]


def _plan(command: str, files: IndexFiles) -> _Plan:
    cleaned, quiets = _split_command(command)
    heads = [st[0] for st in cleaned]
    if len(cleaned) == 1:
        if heads[0] == "wc":
            return _plan_wc(cleaned[0], command, files)
        if heads[0] != "grep":
            raise _Decline
        g = _parse_grep(cleaned[0], command, None, stage_only=False)
        g.quiet = quiets[0]
        if len(g.operands) != 1:
            raise _Decline
        return _plan_single(g, _operand_ix(g.operands[0], files, command))
    if cleaned[-1] != ["wc", "-l"] or any(h != "grep" for h in heads[1:-1]):
        raise _Decline
    if heads[0] == "grep":
        g = _parse_grep(cleaned[0], command, None, stage_only=False)
        g.quiet = quiets[0]
        if not g.operands:
            raise _Decline
        ops = [_operand_ix(t, files, command) for t in g.operands]
        return _plan_grep_pipeline(g, ops, cleaned, command)
    if heads[0] == "find":
        lines = _find_names(cleaned[0], command, files, quiets[0])
    elif heads[0] == "ls":
        lines = _ls_names(cleaned[0], command, files, quiets[0])
    else:
        raise _Decline
    for parts in cleaned[1:-1]:
        lines = _filter_stage(parts, command, lines, None)
    answer = (0, f"{len(lines)}\n", "")
    return _Plan([], [], lambda get, rawget: answer)


def evaluate_many(commands: list, files: IndexFiles, store: Optional[MatchStore],
                  read_budget_s: float, force_first: bool = True) -> dict:
    """`{command: Ran | None | PENDING}` for each command, answered from the git index world.

    None declines (the caller spawns or reads UNDECIDABLE); PENDING means `read_budget_s` of
    process time ran out before every blob the command needs was matched, and a later call
    resumes from the recorded per-(pattern, oid) counts. Every command is parsed once, the oids
    lacking a record for any pending pattern are fetched in ONE `read_blobs` per slice, and each
    blob is tested against every pattern that needs it. With `force_first` at least one slice
    runs whatever the budget, so coverage advances however small it is; without it a spent
    budget reads no blob. An operand with no index entry reads as absent: the caller classifies
    untracked operands before asking. Never spawns itself."""
    plans: dict = {}
    out: dict = {}
    for command in dict.fromkeys(commands):
        try:
            plans[command] = _plan(command, files)
        except Exception:  # noqa: BLE001 -- any doubt is a decline
            out[command] = None
    memo: dict = {}
    todo: dict = {}
    wants_raw: set = set()
    for plan in plans.values():
        for m, oid in plan.needs:
            if (m.key, oid) in memo or m.key in todo.get(oid, ()):
                continue
            v = store.matched(m.key, oid) if store is not None else None
            if _valid(v):
                memo[(m.key, oid)] = v
            else:
                todo.setdefault(oid, {})[m.key] = m
        for oid in plan.raw:
            wants_raw.add(oid)
            todo.setdefault(oid, {})
    rawdata: dict = {}
    failed: set = set()
    order = list(todo)
    started = _cpu_s()
    for i in range(0, len(order), _SLICE):
        if (i or not force_first) and _cpu_s() - started >= read_budget_s:
            break
        batch = order[i:i + _SLICE]
        try:
            blobs = files.read_blobs(batch)
        except Exception:  # noqa: BLE001
            failed.update(batch)
            continue
        fresh: dict = {}
        for oid in batch:
            data = blobs.get(oid)
            if data is None:
                failed.add(oid)
                continue
            try:
                packed = {key: _pack(m.scan(data)) for key, m in todo[oid].items()}
            except Exception:  # noqa: BLE001
                failed.add(oid)
                continue
            for key, v in packed.items():
                memo[(key, oid)] = v
                fresh.setdefault(key, {})[oid] = v
            if oid in wants_raw:
                rawdata[oid] = data
        if store is not None:
            for key, counts in fresh.items():
                store.put_matched(key, counts)

    def get(key: str, oid: str) -> tuple:
        return _unpack(memo[(key, oid)])

    for command, plan in plans.items():
        state = "ready"
        for m, oid in plan.needs:
            if (m.key, oid) not in memo:
                state = "declined" if oid in failed else "pending"
                if state == "declined":
                    break
        if state != "declined":
            for oid in plan.raw:
                if oid not in rawdata:
                    state = "declined" if oid in failed else "pending"
                    if state == "declined":
                        break
        if state == "declined":
            out[command] = None
        elif state == "pending":
            out[command] = PENDING
        else:
            try:
                out[command] = plan.finish(get, rawdata.__getitem__)
            except Exception:  # noqa: BLE001
                out[command] = None
    return {c: out[c] for c in dict.fromkeys(commands)}
