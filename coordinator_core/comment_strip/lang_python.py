from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass


@dataclass
class Candidate:
    line_start: int
    line_end: int
    text: str
    kind: str
    sole_body: bool = False
    indent: str = ""


def _docstring_expr_lines(tree: ast.AST) -> dict[tuple[int, int], tuple[bool, str]]:
    spans: dict[tuple[int, int], tuple[bool, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ) and isinstance(body[0].value.value, str):
                first = body[0]
                sole = len(body) == 1 and not isinstance(node, ast.Module)
                indent = " " * (first.col_offset if sole else 0)
                spans[(first.lineno, getattr(first, "end_lineno", first.lineno))] = (sole, indent)
    return spans


def collect_candidates(text: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    doc_spans = _docstring_expr_lines(tree)
    lines = text.splitlines(keepends=True)
    for (start, end), (sole, indent) in sorted(doc_spans.items()):
        seg = "".join(lines[start - 1:end])
        candidates.append(Candidate(start, end, seg, "docstring", sole_body=sole, indent=indent))
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenizeError:
        return candidates
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            s, e = tok.start[0], tok.end[0]
            candidates.append(Candidate(s, e, tok.string, "comment"))
    return candidates


def _strip_ast_docstrings(tree: ast.AST, removed_line_starts: set[int] | None = None) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ) and isinstance(body[0].value.value, str):
                if removed_line_starts is not None and body[0].lineno not in removed_line_starts:
                    continue
                del body[0]
                if not body and not isinstance(node, ast.Module):
                    body.append(ast.Pass())


def proof_equivalent(original_text: str, new_text: str, removed_docstring_line_starts: set[int] | None = None) -> bool:
    try:
        orig_tree = ast.parse(original_text)
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return False
    _strip_ast_docstrings(orig_tree, removed_docstring_line_starts)
    ast.fix_missing_locations(orig_tree)
    return ast.dump(orig_tree, annotate_fields=False) == ast.dump(new_tree, annotate_fields=False)
