"""In-tree verdict for delivery-note reach claims: a symbol-keyed import-closure scan.

`judge_reach_claims` decides each "`X` reaches `Y`" claim against the tree the commit lands, in
process with at most one git spawn: one `read_index`, one `git grep --cached -o -P` that matches the
definition shapes itself and names the indexed files defining a claimed symbol, bytes reads of the
landing paths (scanned only on lines naming a symbol), and (for withheld paths only)
`authoring_leaks.HeadView` blob reads. No other indexed file is read to find definitions; an indexed
file dirty in the worktree but neither landing nor withheld is matched on its index content.
Definition shapes are single-line, in both the grep and the landing/withheld scan.

Negative-spec:
  - `unreachable` only when X and Y are both defined in a production file, the import closure from
    X's definition files resolved fully within `REACH_CLOSURE_DEPTH` hops, and no file in it holds
    a dynamic-dispatch marker. Everything the scan does not model is `undecidable`, because a false
    `unreachable` fails a run.
  - Test files are never scanned: a symbol defined or named only there is `undecidable`.
  - A landing path is read from the worktree; a withheld path is read at HEAD (its worktree edit does
    not land); a deleted path reads absent.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit.reach_claims import ReachClaim, ReachVerdict
from coordinator_core.ops.review_reachability import _CODE_EXT, _TEST_RE, _WORD

# How many import hops from X's definition files a claim is traced before it is undecidable:
# "does X's module pull in a file naming Y within this many imports". A path deeper than this is a
# truncated closure, never a fully resolved one.
REACH_CLOSURE_DEPTH = 3

_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs")
_ASSET_EXT = (".css", ".scss", ".sass", ".less", ".json", ".svg", ".png", ".jpg", ".gif", ".md", ".html")
_PY_MARKERS = (b"getattr(", b"importlib", b"__import__")
_JS_DYNAMIC = re.compile(rb"(?:\bimport|\brequire)\s*\(\s*[^'\"\s)]")
_PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", re.M)
_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]+as[ \t]+\w+)?(?:[ \t]*,[ \t]*[\w.]+(?:[ \t]+as[ \t]+\w+)?)*)", re.M)
_JS_SPEC = re.compile(rb"""(?:\bfrom|\bimport|\brequire\s*\(|\bimport\s*\()\s*['"]([^'"]+)['"]""")
_RS_MOD = re.compile(rb"^\s*(?:pub\s+)?(?:mod\s+\w+\s*;|use\s+(?:crate|super|self)::)", re.M)
_DEF_PREFIX = r"(?:def|class|function\*?|const|let|var|interface|type|enum|fn|struct|trait)"


def _norm(p: str) -> str:
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _prod(p: str) -> bool:
    return p.endswith(_CODE_EXT) and not _TEST_RE.search(p)


def _has_word(b: bytes, word: bytes) -> bool:
    i = b.find(word)
    while i != -1:
        j = i + len(word)
        if not ((i and _WORD.match(b, i - 1)) or _WORD.match(b, j)):
            return True
        i = b.find(word, j)
    return False


def _split_target(name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(symbol, file, module hint) of a claim side; symbol None when the side names a file."""
    if ":" in name:
        file_part, sym = name.rsplit(":", 1)
        return sym, _norm(file_part), None
    if name.endswith(_CODE_EXT):
        return None, _norm(name), None
    if "." in name:
        head, sym = name.rsplit(".", 1)
        return sym, None, head.replace(".", "/")
    return name, None, None


def _candidates(name: str) -> list[tuple[Optional[str], Optional[str], Optional[str]]]:
    """Readings of a claim side: the split form, then (for a dotted name) the whole name as a
    registered literal such as an op name."""
    out = [_split_target(name)]
    if out[0][2] is not None:
        out.append((name, None, None))
    return out


_DEF_SHAPES = (
    r"(?:\b" + _DEF_PREFIX + r"\s+{s}\b)",
    r"(?:^[ \t]*(?:export\s+)?(?:async\s+)?{s}\s*[:=(])",
    r"(?:\b(?:register\w*|tool|op|command)\s*\(\s*(?:name\s*=\s*)?['\"]{s}['\"])",
    r"(?:\bname\s*[=:]\s*['\"]{s}['\"])",
)


def _esc(sym: str) -> str:
    return re.escape(sym.encode()).decode("latin-1")


def _def_re(sym: str) -> re.Pattern:
    """Definition shapes of one symbol; every shape is confined to one line."""
    pat = "|".join(shape.replace("{s}", _esc(sym)) for shape in _DEF_SHAPES)
    return re.compile(pat.encode("latin-1"), re.M)


def _grep_patterns(syms: Iterable[str]) -> tuple[bytes, re.Pattern]:
    """(PCRE text for `git grep -P`, regex naming the symbol inside one `-o` match) over `syms`."""
    alt = "|".join(_esc(s) for s in syms)
    plain = "|".join(shape.replace("{s}", f"(?:{alt})") for shape in _DEF_SHAPES)
    named = "|".join(shape.replace("{s}", f"(?P<g{i}>{alt})") for i, shape in enumerate(_DEF_SHAPES))
    return plain.encode("latin-1"), re.compile(named.encode("latin-1"), re.M)


def _line_scan(b: bytes, tok: bytes, pat: re.Pattern) -> bool:
    """`pat` matched on a line of `b` containing `tok`; other lines are never run through `pat`."""
    i = b.find(tok)
    scanned = -1
    while i != -1:
        if i >= scanned:
            start = b.rfind(b"\n", 0, i) + 1
            end = b.find(b"\n", i)
            end = len(b) if end == -1 else end
            if pat.search(b, start, end):
                return True
            scanned = end
        i = b.find(tok, i + 1)
    return False


class _Tree:
    def __init__(self, root: Path, files: set[str], withheld_only: set[str], head) -> None:
        self.root = root
        self.files = files
        self.withheld_only = withheld_only
        self.head = head
        self.dirs: set[str] = set()
        for f in files:
            d = os.path.dirname(f)
            while d and d not in self.dirs:
                self.dirs.add(d)
                d = os.path.dirname(d)
        self.cache: dict[str, bytes] = {}
        self.info: dict[str, tuple[list[str], list[str], list[str]]] = {}

    def bytes(self, rel: str) -> bytes:
        got = self.cache.get(rel)
        if got is not None:
            return got
        data: Optional[bytes]
        if rel in self.withheld_only:
            data = self.head.head_bytes(rel) if self.head is not None else None
        else:
            try:
                data = (self.root / rel).read_bytes()
            except OSError:
                data = None
        self.cache[rel] = data or b""
        return self.cache[rel]

    def _py_resolve(self, base: str, parts: list[str]) -> tuple[Optional[str], bool]:
        stem = "/".join(p for p in ([base] if base else []) + parts)
        for cand in (stem + ".py", stem + "/__init__.py"):
            if cand in self.files:
                return cand, True
        return None, stem in self.dirs

    def _py_imports(self, rel: str, text: str) -> tuple[list[str], list[str]]:
        targets: list[str] = []
        unresolved: list[str] = []
        ancestors = [""]
        d = os.path.dirname(rel)
        chain = []
        while d:
            chain.append(d)
            d = os.path.dirname(d)
        ancestors += list(reversed(chain))

        def absolute(parts: list[str], names: list[str]) -> None:
            top = parts[0]
            bases = [b for b in ancestors if (f"{b}/{top}.py" if b else f"{top}.py") in self.files
                     or (f"{b}/{top}" if b else top) in self.dirs]
            if not bases:
                return
            found = False
            for base in bases:
                mod, is_dir = self._py_resolve(base, parts)
                if mod:
                    targets.append(mod)
                found = found or bool(mod) or is_dir
                for n in names:
                    sub, _ = self._py_resolve(base, parts + [n])
                    if sub:
                        targets.append(sub)
                        found = True
                if found:
                    break
            if not found:
                unresolved.append(".".join(parts))

        for m in _PY_FROM.finditer(text):
            dots, mod, rest = m.group(1), m.group(2), m.group(3)
            names = [re.split(r"\s+as\s+", n.strip())[0] for n in re.sub(r"#[^\n]*", "", rest.strip("() \t\n")).replace("\n", " ").split(",")]
            names = [n for n in names if re.fullmatch(r"\w+", n)]
            parts = [p for p in mod.split(".") if p]
            if dots:
                up = os.path.dirname(rel)
                for _ in range(len(dots) - 1):
                    up = os.path.dirname(up)
                if parts:
                    modf, is_dir = self._py_resolve(up, parts)
                else:
                    pkg_init = f"{up}/__init__.py" if up else "__init__.py"
                    modf, is_dir = (pkg_init if pkg_init in self.files else None), True
                if modf:
                    targets.append(modf)
                found = bool(modf) or is_dir
                for n in names:
                    sub, _ = self._py_resolve(up, parts + [n])
                    if sub:
                        targets.append(sub)
                        found = True
                if not found:
                    unresolved.append(dots + mod)
            elif parts:
                absolute(parts, names)
        for m in _PY_IMPORT.finditer(text):
            for item in m.group(1).split(","):
                parts = [p for p in re.split(r"\s+as\s+", item.strip())[0].split(".") if p]
                if parts:
                    absolute(parts, [])
        return targets, unresolved

    def _js_imports(self, rel: str, data: bytes) -> tuple[list[str], list[str]]:
        targets: list[str] = []
        unresolved: list[str] = []
        base_dir = os.path.dirname(rel)
        for m in _JS_SPEC.finditer(data):
            spec = m.group(1).decode("utf-8", "replace")
            if spec.startswith(("@/", "~/", "#")):
                unresolved.append(spec)
                continue
            if not spec.startswith("."):
                continue
            if spec.endswith(_ASSET_EXT):
                continue
            base = _norm(os.path.normpath(os.path.join(base_dir, spec)))
            cands = [base]
            if base.endswith((".js", ".jsx", ".mjs")):
                stem = base.rsplit(".", 1)[0]
                cands += [stem + e for e in (".ts", ".tsx")]
            cands += [base + e for e in _JS_EXT] + [f"{base}/index{e}" for e in _JS_EXT]
            hit = next((c for c in cands if c in self.files), None)
            if hit:
                targets.append(hit)
            else:
                unresolved.append(spec)
        return targets, unresolved

    def imports(self, rel: str) -> tuple[list[str], list[str], list[str]]:
        """(resolved in-tree targets, unresolved in-repo imports, dynamic markers) of one file."""
        got = self.info.get(rel)
        if got is not None:
            return got
        data = self.bytes(rel)
        markers: list[str] = []
        if rel.endswith(".py"):
            markers = [mk.decode() for mk in _PY_MARKERS if mk in data]
            targets, unresolved = self._py_imports(rel, data.decode("utf-8", "replace"))
        elif rel.endswith(_JS_EXT):
            if _JS_DYNAMIC.search(data):
                markers = ["dynamic import()/require()"]
            targets, unresolved = self._js_imports(rel, data)
        else:
            targets, unresolved = [], []
            if _RS_MOD.search(data):
                unresolved = ["rust module graph"]
        res = (list(dict.fromkeys(targets)), unresolved, markers)
        self.info[rel] = res
        return res


def _definers(tree: _Tree, defs: dict[str, list[str]], sym: str, file: Optional[str], hint: Optional[str]) -> list[str]:
    found = defs.get(sym, [])
    if file is not None:
        return [f for f in found if f == file]
    if hint:
        narrowed = [f for f in found if f.rsplit(".", 1)[0].endswith(hint) or f.endswith(f"{hint}/__init__.py")]
        return narrowed or found
    return found


def _judge_one(tree: _Tree, claim: ReachClaim, defs: dict[str, list[str]]) -> ReachVerdict:
    def verdict(v: str, why: str) -> ReachVerdict:
        return ReachVerdict(claim, v, why)

    sides = []
    for name in (claim.source, claim.target):
        owners: list[str] = []
        for sym, file, hint in _candidates(name):
            if sym is None:
                owners = [file] if file in tree.files else []
            else:
                owners = _definers(tree, defs, sym, file, hint)
            if owners:
                break
        if not owners:
            return verdict("undecidable", f"`{name}` is not defined in a production file of the tree")
        sides.append((sym, file, owners))
    (xsym, _, xdefs), (ysym, yfile, ydefs) = sides
    ybytes = ysym.encode() if ysym is not None else None

    visited = set(xdefs)
    frontier = list(xdefs)
    unresolved: list[str] = []
    markers: list[tuple[str, str]] = []
    truncated = False
    for depth in range(REACH_CLOSURE_DEPTH + 1):
        nxt: list[str] = []
        for f in frontier:
            if ybytes is None:
                if f in ydefs:
                    return verdict("reachable", f"`{claim.source}` imports `{claim.target}`")
            elif _has_word(tree.bytes(f), ybytes):
                return verdict("reachable", f"`{ysym}` is named in {f}")
            targets, unres, marks = tree.imports(f)
            unresolved += [f"{f}: {u}" for u in unres]
            markers += [(f, mk) for mk in marks]
            for t in targets:
                if t not in visited:
                    visited.add(t)
                    nxt.append(t)
        if depth == REACH_CLOSURE_DEPTH:
            truncated = bool(nxt)
            break
        frontier = nxt
        if not frontier:
            break
    if truncated:
        return verdict("undecidable", f"a path to `{claim.target}` may lie deeper than {REACH_CLOSURE_DEPTH} import hops")
    if unresolved:
        return verdict("undecidable", f"unresolved in-repo import ({unresolved[0]})")
    if markers:
        return verdict("undecidable", f"dynamic dispatch ({markers[0][1]}) in {markers[0][0]}")
    return verdict(
        "unreachable",
        f"no file within {REACH_CLOSURE_DEPTH} import hops of {xdefs[0]} names `{claim.target}`"
        f" (defined in {ydefs[0]})",
    )


def judge_reach_claims(
    root: Path,
    claims: Iterable[ReachClaim],
    *,
    landing_paths: Iterable[str] = (),
    deleted_paths: Iterable[str] = (),
    withheld_paths: Iterable[str] | Mapping[str, str] = (),
) -> tuple[ReachVerdict, ...]:
    """One verdict per claim over the tree the commit lands: the index plus `landing_paths`, minus
    `deleted_paths`; `withheld_paths` (dirty, not landing) read at HEAD, never from the worktree."""
    claims = tuple(claims)
    if not claims:
        return ()
    root = Path(root)
    try:
        from coordinator_core.git.git_state import read_index

        indexed = set(read_index(root))
    except Exception as exc:
        why = f"tree index unreadable ({type(exc).__name__})"
        return tuple(ReachVerdict(c, "undecidable", why) for c in claims)

    landing = {_norm(p) for p in landing_paths}
    deleted = {_norm(p) for p in deleted_paths}
    from coordinator_core.ops.dispatch_emit.shipped_claims import content_withheld

    withheld_only = {_norm(p) for p in content_withheld(withheld_paths)} - landing
    withheld_only = {p for p in withheld_only if _prod(p)}
    head = None
    if withheld_only:
        try:
            from coordinator_core.authoring_leaks import HeadView

            head = HeadView(root, withheld_only)
        except Exception:
            head = None
    present = (indexed | landing) - deleted
    for w in withheld_only:
        if head is not None and head.in_head(w) and w not in deleted:
            present.add(w)
        else:
            present.discard(w)
    files = {p for p in present if _prod(p)}
    tree = _Tree(root, files, withheld_only, head)

    syms: dict[str, re.Pattern] = {}
    for c in claims:
        for name in (c.source, c.target):
            for sym, _, _ in _candidates(name):
                if sym is not None:
                    syms.setdefault(sym, _def_re(sym))
    tokens = [(s.encode(), pat, s) for s, pat in syms.items()]
    defs: dict[str, list[str]] = {s: [] for s in syms}
    if tokens:
        direct = (landing | withheld_only) & files
        hits = _index_def_hits(root, list(syms))
        if hits is None:
            why = "tree index unsearchable (git grep failed)"
            return tuple(ReachVerdict(c, "undecidable", why) for c in claims)
        for f, named in hits.items():
            if f in files and f not in direct:
                for s in named:
                    defs[s].append(f)
        for f in direct:
            b = tree.bytes(f)
            found = False
            for tok, pat, s in tokens:
                if tok in b and _line_scan(b, tok, pat):
                    defs[s].append(f)
                    found = True
            if not found:
                tree.cache.pop(f, None)
        for lst in defs.values():
            lst.sort()
    return tuple(_judge_one(tree, c, defs) for c in claims)


def _index_def_hits(root: Path, syms: list[str]) -> Optional[dict[str, set[str]]]:
    """Indexed code file -> symbols it defines, from one `git grep --cached -o -P` that matches the
    definition shapes itself; None when git failed.

    Patterns go through stdin (`-f -`) so the Windows argv cap never bounds the symbol count.
    `-P` needs a git built with PCRE; without it git exits 128 and the judge is `undecidable`, never
    a wrong verdict.
    """
    from coordinator_core.git.run import run_git

    plain, named = _grep_patterns(syms)
    by_bytes = {s.encode(): s for s in syms}
    pathspec = [f"*{ext}" for ext in _CODE_EXT]
    res = run_git(
        ["-C", str(root), "grep", "--cached", "-a", "-o", "-z", "-P", "-f", "-", "--", *pathspec],
        input=plain + b"\n",
    )
    if res.timed_out or res.returncode not in (0, 1):
        return None
    out: dict[str, set[str]] = {}
    for line in res.stdout_bytes.split(b"\n"):
        path, sep, text = line.partition(b"\0")
        if not sep:
            continue
        m = named.search(text)
        if m is not None:
            sym = by_bytes.get(m.group(m.lastgroup))
            if sym is not None:
                out.setdefault(_norm(path.decode("utf-8", "replace")), set()).add(sym)
    return out
