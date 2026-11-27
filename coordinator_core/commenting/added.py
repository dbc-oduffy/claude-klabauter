"""Added-comment detector: comments and multi-line docstrings present in `after` and absent from `before`."""
from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass

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


def _python(text: str) -> list[_Found]:
    lines = text.splitlines()
    found = []
    for c in lang_python.collect_candidates(text):
        raw = c.text.rstrip("\r\n")
        if c.kind == "docstring":
            found.append(_Found(c.line_start, raw, "docstring", False, c.line_end > c.line_start))
        else:
            standalone = lines[c.line_start - 1].lstrip().startswith("#")
            found.append(_Found(c.line_start, raw.rstrip(), "comment", standalone, False))
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


def _lex(suffix: str, text: str) -> list[_Found]:
    if suffix == ".py":
        return _python(text)
    if suffix in _CLIKE:
        return _spans(text, lang_clike.find_comment_spans(text))
    return _spans(text, lang_hash.find_comment_spans(text, shell_heredocs=suffix in _SHELL))


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


def added_comments(rel_path: str, before: str, after: str) -> list[AddedComment]:
    """Comments added between `before` and `after`; suffixes outside SUFFIXES, exempt paths, generated files and lexer failures return []."""
    suffix = _suffix(rel_path)
    if suffix not in SUFFIXES or is_exempt_path(rel_path) or _is_fixture_path(rel_path):
        return []
    if is_generated_file(after):
        return []
    try:
        if suffix == ".py" and before.strip():
            ast.parse(before)
        after_found = _lex(suffix, after)
        before_found = _lex(suffix, before)
    except Exception:
        return []
    seen = Counter(_identity(f) for f in before_found)
    new: list[_Found] = []
    for f in after_found:
        key = _identity(f)
        if seen[key] > 0:
            seen[key] -= 1
        else:
            new.append(f)
    new_ids = {id(f) for f in new}
    return [
        AddedComment(rel_path, f.line, f.text, f.kind)
        for f in _judged(after_found)
        if id(f) in new_ids
    ]
