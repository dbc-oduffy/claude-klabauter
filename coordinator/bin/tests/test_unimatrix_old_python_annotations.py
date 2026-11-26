"""Guard: every unimatrix script and _unimatrix module imports under a pre-3.10 python3.

Each file carries `from __future__ import annotations` as its first statement after the module
docstring, and evaluates no PEP 604 union, `match` statement or `isinstance(x, A | B)` at runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
FILES = sorted(BIN.glob("unimatrix-*.py")) + sorted((BIN / "_unimatrix").rglob("*.py"))


_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type", "Optional", "Union", "List", "Dict", "Tuple", "Set"}


def _is_union_operand(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Subscript):
        base = node.value
        return getattr(base, "id", getattr(base, "attr", "")) in _GENERICS
    return False


class _RuntimeUnions(ast.NodeVisitor):
    """Collects 3.10-only syntax that `from __future__ import annotations` cannot defer."""

    def __init__(self) -> None:
        self.hits: list[str] = []

    def _def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        for d in node.args.defaults + [k for k in node.args.kw_defaults if k is not None]:
            self.visit(d)
        for stmt in node.body:
            self.visit(stmt)

    visit_FunctionDef = _def
    visit_AsyncFunctionDef = _def

    def _isinstance_union(self, node: ast.Call) -> None:
        if len(node.args) == 2 and isinstance(node.args[1], ast.BinOp) and isinstance(node.args[1].op, ast.BitOr):
            self.hits.append(f"line {node.lineno}: isinstance with a `|` union")

    def visit_Call(self, node: ast.Call) -> None:
        if getattr(node.func, "id", "") in ("isinstance", "issubclass"):
            self._isinstance_union(node)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.hits.append(f"line {node.lineno}: match statement")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.BitOr) and (_is_union_operand(node.left) or _is_union_operand(node.right)):
            self.hits.append(f"line {node.lineno}: PEP 604 union evaluated at runtime")
        self.generic_visit(node)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_census_finds_the_unimatrix_files() -> None:
    names = {p.name for p in FILES}
    assert "unimatrix-autolink.py" in names
    assert "protocol.py" in names


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(BIN)))
def test_future_annotations_follows_the_docstring(path: Path) -> None:
    body = _parse(path).body
    i = 1 if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
        and isinstance(body[0].value.value, str) else 0
    stmt = body[i] if len(body) > i else None
    ok = (
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(a.name == "annotations" for a in stmt.names)
    )
    assert ok, f"{path.name}: first statement after the docstring must be `from __future__ import annotations`"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(BIN)))
def test_no_runtime_pep604_union_or_match(path: Path) -> None:
    visitor = _RuntimeUnions()
    for stmt in _parse(path).body:
        visitor.visit(stmt)
    assert not visitor.hits, f"{path.name}: 3.10-only syntax at runtime: {visitor.hits}"


def test_the_detector_flags_a_module_level_union_and_match() -> None:
    bad = ast.parse("X = int | None\nmatch 1:\n    case _:\n        pass\nisinstance(1, int | str)\n")
    visitor = _RuntimeUnions()
    for stmt in bad.body:
        visitor.visit(stmt)
    assert len(visitor.hits) >= 3


def test_the_detector_passes_a_deferred_annotation() -> None:
    ok = ast.parse("def f(x: int | None = None) -> str | None:\n    return None\n")
    visitor = _RuntimeUnions()
    for stmt in ok.body:
        visitor.visit(stmt)
    assert not visitor.hits
