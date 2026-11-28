"""Added-comment detector: comments and multi-line docstrings present in `after` and absent from `before`."""
from __future__ import annotations

import ast
import hashlib
import io
import re
import threading
import tokenize
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

from coordinator_core.attribution import is_exempt_path
from coordinator_core.comment_strip import lang_clike, lang_hash, lang_python

from .added_types import SUFFIXES, AddedComment, CommentKind
from .allow import (
    has_tag,
    is_allowed_comment,
    is_allowed_docstring,
    is_generated_file,
    strip_marker,
)
from .detector import _is_fixture_path

_CLIKE = frozenset({".ts", ".tsx", ".js", ".mjs", ".cpp", ".h", ".hpp", ".cs", ".jsonc"})
_SHELL = frozenset({".sh", ".bash"})
_DOCSTRING_QUOTES_RE = re.compile(r"""^[rRuUbBfF]*(?:\"\"\"|'''|\"|')|(?:\"\"\"|'''|\"|')$""")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _Found:
    line: int
    text: str
    kind: CommentKind
    standalone: bool
    multiline: bool


def _suffix(rel_path: str) -> str:
    name = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


_BODY_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def _docstring_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """Docstring line spans, visiting statement lists only: defs never nest inside expressions."""
    spans: set[tuple[int, int]] = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(
                first.value.value, str
            ):
                spans.add((first.lineno, first.end_lineno or first.lineno))
        for name in _BODY_FIELDS:
            children = getattr(node, name, None)
            if isinstance(children, list):
                stack.extend(c for c in children if isinstance(c, ast.AST))
    return sorted(spans)


def _python(text: str) -> list[_Found] | None:
    """One parse and one tokenize; None when `text` does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines()
    kept = text.splitlines(keepends=True)
    found = [
        _Found(start, "".join(kept[start - 1:end]).rstrip("\r\n"), "docstring", False, end > start)
        for start, end in _docstring_spans(tree)
    ]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                line = tok.start[0]
                found.append(_Found(line, tok.string.rstrip(), "comment", lines[line - 1].lstrip().startswith("#"), False))
    except tokenize.TokenError:
        pass
    return found


def _spans(text: str, spans) -> list[_Found]:
    found = []
    for s in spans:
        line = text.count("\n", 0, s.start) + 1
        line_start = text.rfind("\n", 0, s.start) + 1
        found.append(
            _Found(line, s.text.strip(), "comment", not text[line_start:s.start].strip(), False)
        )
    return found


def _lex(suffix: str, text: str) -> list[_Found] | None:
    """Comments in `text`; None when a `.py` text does not parse."""
    if suffix == ".py":
        return _python(text)
    if suffix in _CLIKE:
        return _spans(text, lang_clike.find_comment_spans(text))
    return _spans(text, lang_hash.find_comment_spans(text, shell_heredocs=suffix in _SHELL))


_LEX_CACHE_MAX = 256
_lex_cache: "OrderedDict[tuple[str, bytes], tuple[_Found, ...] | None]" = OrderedDict()
_lex_cache_lock = threading.Lock()


def _lexed(suffix: str, text: str) -> tuple[_Found, ...] | None:
    """`_lex` memoised per content, so a file is lexed once however many callers ask for it."""
    if not text:
        return ()
    key = (suffix, hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest())
    with _lex_cache_lock:
        if key in _lex_cache:
            _lex_cache.move_to_end(key)
            return _lex_cache[key]
    found = _lex(suffix, text)
    result = None if found is None else tuple(found)
    with _lex_cache_lock:
        _lex_cache[key] = result
        if len(_lex_cache) > _LEX_CACHE_MAX:
            _lex_cache.popitem(last=False)
    return result


def _identity(f: _Found) -> tuple[str, str]:
    if f.kind == "docstring":
        body = _DOCSTRING_QUOTES_RE.sub("", f.text.strip())
    else:
        body = strip_marker(f.text)
    return f.kind, _WS_RE.sub(" ", body).strip()


def _docstring_body(f: _Found) -> str:
    return _DOCSTRING_QUOTES_RE.sub("", f.text.strip()).strip()


def _judged(found: list[_Found]) -> list[_Found]:
    """Comments the lint would flag: untagged, non-tooling, with tag-block continuation lines inherited."""
    flagged = []
    prev_tagged_line = 0
    for f in found:
        if f.kind == "docstring":
            if f.multiline and not is_allowed_docstring(_docstring_body(f)):
                flagged.append(f)
            continue
        tagged = has_tag(f.text) or (
            f.standalone and prev_tagged_line == f.line - 1 and prev_tagged_line != 0
        )
        if tagged:
            prev_tagged_line = f.line if f.standalone else 0
            continue
        prev_tagged_line = 0
        if not is_allowed_comment(f.text):
            flagged.append(f)
    return flagged


def added_comments(
    rel_path: str,
    before: str,
    after: str,
    *,
    keep: Optional[Callable[[str], bool]] = None,
) -> list[AddedComment]:
    """Comments added between `before` and `after`; suffixes outside SUFFIXES, exempt paths, generated files and lexer failures return [].

    `before` is lexed only when a flagged comment of `after` (that `keep` accepts, when given) survives to be compared against it."""
    suffix = _suffix(rel_path)
    if suffix not in SUFFIXES or is_exempt_path(rel_path) or _is_fixture_path(rel_path):
        return []
    if is_generated_file(after):
        return []
    try:
        after_found = _lexed(suffix, after)
        if not after_found:
            return []
        flagged = [f for f in _judged(list(after_found)) if keep is None or keep(f.text)]
        if not flagged:
            return []
        before_found = _lexed(suffix, before)
    except Exception:
        return []
    if before_found is None and before.strip():
        return []
    seen = Counter(_identity(f) for f in before_found or ())
    new: list[_Found] = []
    for f in after_found:
        key = _identity(f)
        if seen[key] > 0:
            seen[key] -= 1
        else:
            new.append(f)
    new_ids = {id(f) for f in new}
    return [AddedComment(rel_path, f.line, f.text, f.kind) for f in flagged if id(f) in new_ids]
