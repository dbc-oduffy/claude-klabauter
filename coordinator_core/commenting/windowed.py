"""Comments of an oversize ``.py`` text found by lexing only the top-level definitions around seed lines."""
from __future__ import annotations

import ast
import re
from typing import Callable, Optional

from .added import added_comments
from .added_types import AddedComment

OVERSIZE_CHARS = 50_000
_DEF_START = ("def ", "class ", "async def ", "@")
_TRIPLE_RE = re.compile(r'"""|\'\'\'')
_OPENER_RE = re.compile(r"#|\"\"\"|'''")


def _is_boundary(lines: list[str], k: int) -> bool:
    line = lines[k]
    return line.startswith(_DEF_START) and not (k > 0 and lines[k - 1].startswith("@") and not line.startswith("@"))


def windowed_added_comments(
    path: str,
    text: str,
    is_seed: Callable[[str], bool],
    keep: Optional[Callable[[str], bool]] = None,
) -> Optional[list[AddedComment]]:
    """Comments in the top-level definitions holding a seed line, lexed against an empty before.

    ``is_seed`` takes a stripped line that carries a comment or docstring opener. None when a window cannot be
    delimited soundly (an odd triple-quote count ahead of it, or it does not parse)."""
    lines = text.split("\n")
    seeds = [i for i, ln in enumerate(lines) if _OPENER_RE.search(ln) is not None and is_seed(ln.strip())]
    if not seeds:
        return []
    windows: list[tuple[int, int]] = []
    for i in seeds:
        if windows and i < windows[-1][1]:
            continue
        start = i
        while start > 0 and not _is_boundary(lines, start):
            start -= 1
        end = i + 1
        while end < len(lines) and not _is_boundary(lines, end):
            end += 1
        windows.append((start, end))
    out: list[AddedComment] = []
    prefix_triples = 0
    cursor = 0
    for start, end in windows:
        prefix_triples += len(_TRIPLE_RE.findall("\n".join(lines[cursor:start])))
        if prefix_triples % 2:
            return None
        body = "\n".join(lines[start:end])
        try:
            ast.parse(body)
        except SyntaxError:
            return None
        out.extend(
            AddedComment(c.path, c.line + start, c.text, c.kind)
            for c in added_comments(path, "", body, keep=keep)
        )
        prefix_triples += len(_TRIPLE_RE.findall(body))
        cursor = end
    return out
