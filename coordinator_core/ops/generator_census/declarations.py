"""Column-0 provenance declarations of one module's source text, read without importing it.

`extract` answers "what does this source declare, at column 0, outside any string?".
Only the declaration statements are parsed, never the whole module.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Mapping

DECLARATION_NAMES = (
    "GENERATES",
    "MUTATES",
    "MUTATES_APPEND",
    "GENERATES_EXTERNAL",
    "UNSTAMPED_BY_DESIGN",
)

# A declaration whose value cannot be evaluated; never a skip.
MALFORMED = "__MALFORMED__"

_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?::[^=\n]*)?=(?!=)", re.MULTILINE)
_TOKEN_RE = re.compile(
    r"[()\[\]{}]|(?=[#'\"])(?:"
    r"'''[^'\\]*(?:(?:\\.|'(?!''))[^'\\]*)*(?:'''|\Z)"
    r'|"""[^"\\]*(?:(?:\\.|"(?!""))[^"\\]*)*(?:"""|\Z)'
    r"|'[^'\\\n]*(?:\\.[^'\\\n]*)*'?"
    r'|"[^"\\\n]*(?:\\.[^"\\\n]*)*"?'
    r"|#[^\n]*)",
    re.DOTALL,
)
_NAMES = frozenset(DECLARATION_NAMES)
_MAX_STATEMENT_LINES = 400


@dataclass(frozen=True)
class Declarations:
    """One module's declared provenance, one field per DECLARATION_NAMES entry, None when absent."""

    generates: object = None
    mutates: object = None
    mutates_append: object = None
    generates_external: object = None
    unstamped_by_design: object = None


def _enclosed(text: str, stop: int) -> list[tuple[int, int]]:
    """Top-level string and bracket spans `(start, end)` through the statement at `stop`, in order.

    An offset `o` lies inside a span when `start < o < end`. An unterminated single-quoted
    string ends at its line; an unterminated triple or bracket runs to EOF.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    opened = 0
    free = 0  # end of the last top-level token: text from here to the next token is code
    for m in _TOKEN_RE.finditer(text):
        start = m.start()
        if depth == 0 and start > stop and text.find("\n", max(free, stop), start) >= 0:
            return spans
        tok = m.group()
        if tok in "([{":
            if depth == 0:
                opened = start
            depth += 1
        elif tok in ")]}":
            if depth == 1:
                spans.append((opened, m.end()))
                free = m.end()
            depth = max(0, depth - 1)
        elif depth == 0:
            if tok[0] != "#":
                spans.append((start, m.end()))
            free = m.end()
    if depth:
        spans.append((opened, len(text) + 1))
    return spans


def _statement_end(text: str, off: int, spans: list[tuple[int, int]], k: int) -> int:
    """Offset of the first newline at or after `off` lying outside every span (spans[k:] sorted)."""
    pos = off
    while True:
        nl = text.find("\n", pos)
        if nl < 0:
            return len(text)
        while k < len(spans) and spans[k][1] <= nl:
            k += 1
        if k < len(spans) and spans[k][0] < nl:
            pos = spans[k][1]
            continue
        return nl


def _statements(source: str, wanted: frozenset[str] | None):
    """Yield (name, parsed assignment node) for column-0 assignments, first parse wins per statement.

    A statement that never parses yields (name, None).
    """
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    hits = [m for m in _ASSIGN_RE.finditer(text) if wanted is None or m.group(1) in wanted]
    if not hits:
        return
    spans = _enclosed(text, hits[-1].start())
    lines = text.split("\n")
    k = 0
    for m in hits:
        off = m.start()
        while k < len(spans) and spans[k][1] <= off:
            k += 1
        if k < len(spans) and spans[k][0] < off:
            continue
        idx = text.count("\n", 0, off)
        # Every shorter prefix leaves a string or bracket open, so the chunk ending at the
        # first top-level newline is the first that can parse; the stepwise walk is the
        # fallback for what spans do not model (e.g. backslash continuations).
        stop = _statement_end(text, off, spans, k)
        body = None
        if text.count("\n", off, stop) < _MAX_STATEMENT_LINES:
            try:
                body = ast.parse(text[off:stop]).body
            except SyntaxError:
                pass
        if body is not None:
            yield m.group(1), (body[0] if len(body) == 1 else None)
            continue
        node = None
        for end in range(idx + 1, min(len(lines), idx + _MAX_STATEMENT_LINES) + 1):
            try:
                body = ast.parse("\n".join(lines[idx:end])).body
            except SyntaxError:
                continue
            node = body[0] if len(body) == 1 else None
            break
        yield m.group(1), node


def _static_str(node: ast.AST, table: Mapping[str, str]) -> str | None:
    """Fold a literal, a Name, `a + b`, `"sep".join([...])`, or an f-string to a str; else None."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        value = table.get(node.id)
        return value if isinstance(value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            elif isinstance(piece, ast.FormattedValue):
                if piece.conversion not in (-1, None) or piece.format_spec is not None:
                    return None
                resolved = _static_str(piece.value, table)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_str(node.left, table)
        right = _static_str(node.right, table)
        return None if left is None or right is None else left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        sep = _static_str(node.func.value, table)
        pieces = [_static_str(e, table) for e in node.args[0].elts]
        if sep is None or any(p is None for p in pieces):
            return None
        return sep.join(pieces)  # type: ignore[arg-type]
    return None


def _assignment(node: ast.AST | None):
    """(targets, value) of a plain or annotated assignment, else ([], None)."""
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id], node.value
    return [], None


def _string_table(source: str, seed: Mapping[str, str]) -> dict[str, str]:
    """Column-0 string constants of `source`, in order, layered over `seed`."""
    table = dict(seed)
    for _, node in _statements(source, None):
        targets, value = _assignment(node)
        if value is None:
            continue
        resolved = _static_str(value, table)
        if resolved is not None:
            for name in targets:
                table[name] = resolved
    return table


def owned_constants(source: str) -> dict[str, str]:
    """String constants of machinery_paths source text; the caller reads and caches the text."""
    return _string_table(source, {})


def _evaluate(node: ast.AST, table: Mapping[str, str]) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        pass
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for element in node.elts:
            resolved = _static_str(element, table)
            if resolved is None:
                try:
                    out.append(ast.literal_eval(element))
                except (ValueError, SyntaxError):
                    return MALFORMED
            else:
                out.append(resolved)
        return out
    resolved = _static_str(node, table)
    return MALFORMED if resolved is None else resolved


def extract(source: str, owned: Mapping[str, str] | None = None) -> dict[str, object]:
    """Declared names of `source` mapped to their evaluated value or MALFORMED; absent names omitted."""
    found: dict[str, tuple[ast.AST | None]] = {}
    for name, node in _statements(source, _NAMES):
        found.setdefault(name, (node,))
    if not found:
        return {}
    table: Mapping[str, str] | None = None
    result: dict[str, object] = {}
    for name in DECLARATION_NAMES:
        if name not in found:
            continue
        _, value = _assignment(found[name][0])
        if value is None:
            result[name] = MALFORMED
            continue
        try:
            result[name] = ast.literal_eval(value)
            continue
        except (ValueError, SyntaxError):
            pass
        if table is None:
            table = _string_table(source, owned or {})
        result[name] = _evaluate(value, table)
    return result
