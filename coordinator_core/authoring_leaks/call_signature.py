"""call_signature detector: a landing .py may not call a first-party function with arguments that
only an uncommitted (dirty) worktree copy of the callee accepts.

The bind is `coordinator_core.pycallsig`'s, the payload parity gate's own. A call is a finding only
when it fails to bind against the callee's HEAD text and binds against the worktree text, so a call
that fails both ways is publish parity's. Cheap path first: only imports the commit's own added lines
(or their enclosing head lines) mention are followed, from a line scan of the landing text; a callee
whose bytes equal HEAD is skipped unparsed, and the landing file is parsed only when a kept callee
differs from HEAD. A callee in the commit, a new function (import_closure's), a decorated
callee, a splat call, a dynamic target and a call under `pytest.raises(TypeError)` abstain.

Zero spawns.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from coordinator_core.authoring_leaks import HeadView, LeakFinding, added_lines
from coordinator_core.authoring_leaks.import_closure import module_files, prefixes, read_text
from coordinator_core.pyimports import module_name_for_file, resolve_relative_module

LandedText = Callable[[str], Optional[str]]

NAME = "call_signature"
KIND = "call_signature"

_FROM_IMPORT = re.compile(r"^[ \t]*from[ \t]+([.\w]+)[ \t]+import[ \t]*(\([^)]*\)|[^\n]*)", re.MULTILINE)
_PLAIN_IMPORT = re.compile(r"^[ \t]*import[ \t]+([^\n]+)", re.MULTILINE)
_COMMENT = re.compile(r"#[^\n]*")
_NAME = re.compile(r"[A-Za-z_][\w.]*")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def candidates(gate_paths: Sequence[str]) -> Iterable[str]:
    """Gate `.py` paths. The callees are not known without reading the landing text, so `detect` builds its own HeadView."""
    return [p for p in gate_paths if p.endswith(".py")]


def _changed_idents(text: str, old: Optional[str]) -> Optional[Set[str]]:
    """Identifiers on the lines `text` adds over `old`, plus each one's enclosing less-indented head lines (a keyword line of a multi-line call); None when every line counts (no HEAD text)."""
    if old is None:
        return None
    if old == text:
        return set()
    lines = text.splitlines()
    out: Set[str] = set()
    seen: Set[int] = set()
    for lineno, _ in added_lines(old, text):
        i = lineno - 1
        while i >= 0 and i not in seen:
            seen.add(i)
            line = lines[i]
            out.update(_IDENT.findall(line))
            indent = len(line) - len(line.lstrip(" \t"))
            if indent == 0:
                break
            i -= 1
            while i >= 0 and (not lines[i].strip() or lines[i].lstrip().startswith("#") or len(lines[i]) - len(lines[i].lstrip(" \t")) >= indent):
                i -= 1
    return out


def _imported_modules(text: str, rel: str, idents: Optional[Set[str]] = None) -> Set[str]:
    """Dotted modules `text` may import, over-inclusive: every `from X import n` yields X and X.n, no parse.

    With `idents`, only imports whose local name is in it: a call into a module the commit's own lines never mention cannot be the commit's leak.
    """
    text = _COMMENT.sub("", text.replace("\\\n", " "))
    mods: Set[str] = set()
    is_pkg = rel.rsplit("/", 1)[-1] == "__init__.py"
    for m in _FROM_IMPORT.finditer(text):
        raw, names = m.group(1), m.group(2)
        level = len(raw) - len(raw.lstrip("."))
        module = raw[level:]
        if level:
            base = resolve_relative_module(module, level, module_name_for_file(rel), is_pkg)
        else:
            base = module
        if not base:
            continue
        toks = _NAME.findall(names.replace("(", " ").replace(")", " "))
        pairs: List[Tuple[str, str]] = []
        i = 0
        while i < len(toks):
            if i + 2 < len(toks) and toks[i + 1] == "as":
                pairs.append((toks[i], toks[i + 2]))
                i += 3
            else:
                pairs.append((toks[i], toks[i]))
                i += 1
        if idents is None or "*" in names or any(local in idents for _, local in pairs):
            mods.add(base)
        mods.update(f"{base}.{n}" for n, local in pairs if "." not in n and (idents is None or local in idents))
    for m in _PLAIN_IMPORT.finditer(text):
        for part in m.group(1).split(","):
            found = _NAME.match(part.strip())
            if found:
                pieces = found.group(0).split(".")
                tail = part.split(" as ")
                local = tail[1].strip() if len(tail) > 1 else pieces[0]
                if idents is None or local in idents:
                    mods.update(".".join(pieces[: i + 1]) for i in range(len(pieces)))
    return mods


def _dir_entries(root: Path, directory: str, cache: Dict[str, Tuple[frozenset, frozenset]]) -> Tuple[frozenset, frozenset]:
    """`(file names, subdirectory names)` directly under `root/directory`; one listing per directory, none for a directory its parent does not list."""
    if directory not in cache:
        entries: Tuple[frozenset, frozenset] = (frozenset(), frozenset())
        parent, _, leaf = directory.rpartition("/")
        if not directory or leaf in _dir_entries(root, parent, cache)[1]:
            try:
                with os.scandir(root / directory if directory else root) as it:
                    listed = list(it)
                entries = (frozenset(e.name for e in listed if e.is_file()), frozenset(e.name for e in listed if e.is_dir()))
            except OSError:
                pass
        cache[directory] = entries
    return cache[directory]


def _callee_files(
    root: Path, rel: str, text: str, listings: Dict[str, Tuple[frozenset, frozenset]], idents: Optional[Set[str]] = None
) -> Dict[str, str]:
    """`{dotted module: repo-relative file}` for the first-party modules `text` imports (and `idents` mention) that exist in the worktree."""
    pfx = prefixes(rel)
    out: Dict[str, str] = {}
    tops: Dict[str, bool] = {}
    for mod in _imported_modules(text, rel, idents):
        top = mod.partition(".")[0]
        if top not in tops:
            tops[top] = any(
                f"{top}.py" in _dir_entries(root, p.rstrip("/"), listings)[0] or top in _dir_entries(root, p.rstrip("/"), listings)[1]
                for p in pfx
            )
        if not tops[top]:
            continue
        for cand in module_files(mod, pfx):
            directory, _, name = cand.rpartition("/")
            if name in _dir_entries(root, directory, listings)[0]:
                out[mod] = cand
                break
    return out


def _dirty_text(view: HeadView, rel: str) -> Optional[str]:
    """Worktree text of `rel` when it is a tracked file whose bytes differ from HEAD even after CRLF folding; None otherwise.

    One read per callee. A CRLF checkout of an unedited file is not dirty: git's blob is the LF text.
    """
    entry = view.entry(rel)
    if entry is None:
        return None
    try:
        with open(f"{view.root}/{rel}", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    folded = data.replace(b"\r\n", b"\n") if b"\r\n" in data else data
    for body in (folded, data) if folded is not data else (data,):
        if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest() == entry[1]:
            return None
    try:
        return data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return None


_SIG_END = re.compile(r"\)[ \t]*(?:->[^\n:]*)?:")


def _signature_source(text: Optional[str], name: str) -> Optional[str]:
    """`def name(...):` source when `name` is a lone, undecorated line-start def in `text`; None when the whole file must be parsed."""
    if text is None:
        return None
    defs = list(re.finditer(rf"^(?:async[ \t]+)?def[ \t]+{re.escape(name)}[ \t]*\(", text, re.MULTILINE))
    if len(defs) != 1:
        return None
    start = defs[0].start()
    end = _SIG_END.search(text, defs[0].end() - 1)
    if end is None or _decorated_above(text, start):
        return None
    return text[start:end.end()]


def _decorated_above(text: str, start: int) -> bool:
    """Whether the contiguous block above offset `start` holds a column-0 decorator line (continuation, closer and comment lines of a multi-line decorator included)."""
    for line in reversed(text[max(0, start - 2000):start].split("\n")[:-1]):
        if line.startswith("@"):
            return True
        if not line.strip() or line[0] not in " \t)]}#":
            return False
    return False


_DEF_AFTER_NEWLINE = re.compile(r"\n(?:async[ \t]+)?def[ \t]+(\w+)[ \t]*\(")


def _signature_map(text: Optional[str], wanted: Set[str]) -> Dict[str, Optional[str]]:
    """`{function name: signature source}` of line-start defs named in `wanted`; None marks a name that needs a whole-file parse (duplicate, decorated, unterminated)."""
    out: Dict[str, Optional[str]] = {}
    if text is None:
        return out
    text = "\n" + text
    for m in _DEF_AFTER_NEWLINE.finditer(text):
        name = m.group(1)
        if name not in wanted:
            continue
        start = m.start() + 1
        end = _SIG_END.search(text, m.end() - 1)
        out[name] = None if name in out or end is None or _decorated_above(text, start) else text[start:end.end()]
    return out


def _decorated_def(text: Optional[str], name: str) -> bool:
    """Whether a line-start `def name` in `text` has a decorator line above it."""
    if text is None:
        return False
    return any(_decorated_above(text, m.start()) for m in re.finditer(rf"^(?:async[ \t]+)?def[ \t]+{re.escape(name)}[ \t]*\(", text, re.MULTILINE))


def _changed_signatures(head_text: Optional[str], wt_text: Optional[str], wanted: Set[str]) -> Set[str]:
    """Names in `wanted` defined on both sides whose signature differs or cannot be compared without a parse."""
    h, w = _signature_map(head_text, wanted), _signature_map(wt_text, wanted)
    return {n for n in h.keys() & w.keys() if h[n] is None or h[n] != w[n]}


def _imported_names(text: str) -> Set[str]:
    """Every name (alias source included) in a `from X import ...` clause of `text`, no parse."""
    text = _COMMENT.sub("", text.replace("\\\n", " "))
    return {n for m in _FROM_IMPORT.finditer(text) for n in _NAME.findall(m.group(2)) if "." not in n}


def _mentions_call(text: str, names: Set[str], imported: Set[str]) -> bool:
    """Whether `text` imports one of `names` or calls it as an attribute -- the cheap gate before parsing a landing."""
    if not names:
        return False
    if names & imported:
        return True
    return re.search(r"\.(?:" + "|".join(map(re.escape, names)) + r")[ \t]*\(", text) is not None


def _call_snippets(text: str, names: Set[str]) -> Optional[List[Tuple[str, str]]]:
    """`(name, call source)` for every `name(...)` in `text` that is not a def; None when a call's parens do not balance."""
    aliases = {m.group(2): m.group(1) for m in re.finditer(r"\b(" + "|".join(map(re.escape, names)) + r")[ \t]+as[ \t]+(\w+)", text)}
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, names | aliases.keys())) + r")[ \t]*\(")
    out: List[Tuple[str, str]] = []
    for m in pattern.finditer(text):
        if text[max(0, m.start() - 4):m.start()] == "def ":
            continue
        depth, i = 0, m.end() - 1
        while i < len(text):
            depth += (text[i] == "(") - (text[i] == ")")
            if depth == 0:
                break
            i += 1
        else:
            return None
        out.append((aliases.get(m.group(1), m.group(1)), text[m.start():i + 1]))
    return out


def _function(text: Optional[str], name: str, source: Optional[str]) -> Optional[ast.AST]:
    """Top-level function `name` in `text`, parsed from its signature `source` when that parses, else from the whole file."""
    if text is None:
        return None
    if source is not None:
        try:
            node = ast.parse(source + "\n    pass\n").body[0]
        except (SyntaxError, ValueError, IndexError):
            node = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    from coordinator_core.pycallsig import top_level_functions

    try:
        return top_level_functions(ast.parse(text)).get(name)
    except (SyntaxError, ValueError):
        return None


def detect(
    root: Path, gate_paths: Sequence[str], head: HeadView, *, landed_text: Optional[LandedText] = None
) -> List[LeakFinding]:
    """Findings for `gate_paths`. `landed_text(rel)` supplies a gate path's text (None = absent) in place of the worktree's."""
    root = Path(root)
    gate = set(gate_paths)
    landings: List[Tuple[str, str, Dict[str, str]]] = []
    callees: Set[str] = set()
    listings: Dict[str, Tuple[frozenset, frozenset]] = {}
    for rel in sorted(p for p in gate if p.endswith(".py")):
        text = landed_text(rel) if landed_text is not None else read_text(root / rel)
        if text is None:
            continue
        idents = _changed_idents(text, head.head_text(rel))
        if idents is not None and not idents:
            continue
        mods = {m: f for m, f in _callee_files(root, rel, text, listings, idents).items() if f not in gate}
        if mods:
            landings.append((rel, text, mods))
            callees.update(mods.values())
    if not callees:
        return []
    callee_view = HeadView(root, callees)
    wt_texts: Dict[str, Optional[str]] = {}
    for c in sorted(callees):
        dirty_text = _dirty_text(callee_view, c)
        if dirty_text is not None:
            wt_texts[c] = dirty_text
    dirty = set(wt_texts)
    if not dirty:
        return []

    # Deferred: a gate with no dirty callee (most) never loads the bind module.
    from coordinator_core.pycallsig import bind_check, calls_asserting_typeerror, FileImportMap, resolve_call_target

    head_texts: Dict[str, Optional[str]] = {c: callee_view.head_text(c) for c in dirty}
    wanted: Dict[str, Set[str]] = {}
    for rel, text, mods in landings:
        live_files = dirty.intersection(mods.values())
        if live_files:
            idents = set(_IDENT.findall(text))
            for f in live_files:
                wanted.setdefault(f, set()).update(idents)
    changed = {c: _changed_signatures(head_texts[c], wt_texts[c], wanted.get(c, set())) for c in dirty}
    sigs: Dict[Tuple[str, str], Tuple[Optional[ast.AST], Optional[ast.AST]]] = {}

    def signatures(callee: str, fn_name: str) -> Tuple[Optional[ast.AST], Optional[ast.AST]]:
        key = (callee, fn_name)
        if key not in sigs:
            if _decorated_def(head_texts[callee], fn_name) or _decorated_def(wt_texts[callee], fn_name):
                sigs[key] = (None, None)
                return sigs[key]
            h_src = _signature_source(head_texts[callee], fn_name)
            w_src = _signature_source(wt_texts[callee], fn_name)
            if h_src is not None and h_src == w_src:
                sigs[key] = (None, None)
            else:
                sigs[key] = (_function(head_texts[callee], fn_name, h_src), _function(wt_texts[callee], fn_name, w_src))
        return sigs[key]

    def may_leak(text: str, live: Dict[str, str]) -> bool:
        """Cheap pre-check on call snippets alone; True when a full parse of the landing is needed to decide."""
        names = {n for f in live.values() for n in changed[f]}
        snippets = _call_snippets(text, names)
        if snippets is None:
            return True
        for name, src in snippets:
            try:
                call = ast.parse(src, mode="eval").body
            except (SyntaxError, ValueError):
                return True
            if not isinstance(call, ast.Call) or any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                continue
            for f in set(live.values()):
                if name in changed[f]:
                    head_fn, wt_fn = signatures(f, name)
                    if head_fn is None or wt_fn is None or head_fn.decorator_list or wt_fn.decorator_list:
                        continue
                    if bind_check(call, head_fn.args) and not bind_check(call, wt_fn.args):
                        return True
        return False

    findings: List[LeakFinding] = []
    for rel, text, mods in landings:
        imported = _imported_names(text)
        live = {m: f for m, f in mods.items() if f in dirty and _mentions_call(text, changed[f], imported)}
        if not live or not may_leak(text, live):
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        fim = FileImportMap(tree, module_name_for_file(rel), rel.rsplit("/", 1)[-1] == "__init__.py")
        skip = calls_asserting_typeerror(fim.withs)
        for call in fim.calls:
            if id(call) in skip or any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                continue
            target = resolve_call_target(call, fim)
            if target is None or target[1]:
                continue
            dotted = target[0]
            module, _, fn_name = dotted.rpartition(".")
            callee = live.get(module)
            if callee is None:
                continue
            if fn_name not in changed[callee]:
                continue
            head_fn, wt_fn = signatures(callee, fn_name)
            if head_fn is None or wt_fn is None or head_fn.decorator_list or wt_fn.decorator_list:
                continue
            at_head = bind_check(call, head_fn.args)
            if at_head and not bind_check(call, wt_fn.args):
                findings.append(
                    LeakFinding(
                        rel,
                        call.lineno,
                        KIND,
                        f"{dotted} -- {'; '.join(at_head)} at HEAD; the worktree copy accepts it: add {callee} to this commit",
                    )
                )
    return findings
