"""Static Python import parsing shared by percolate and the authoring-time gates.

Import-safe and stdlib-only: one definition of how a file's import
statements, relative-import resolution and module naming are read.
"""

from __future__ import annotations

import ast
from typing import NamedTuple, Optional


class ImportRecord(NamedTuple):
    """One import statement. `module` is the dotted name (`import a.b`) or the
    `from` source (`""` for `from . import x`); `names` are the imported
    aliases of a from-import (empty for a plain import); `level` is the
    relative-import dot count (0 = absolute)."""

    module: str
    names: "tuple[str, ...]"
    level: int
    lineno: int


_DYNAMIC_IMPORT_ATTR_CALLS: "frozenset[tuple[str, str]]" = frozenset(
    {("importlib", "import_module"), ("runpy", "run_module"), ("runpy", "run_path")}
)


def import_test_is_type_checking(test: object) -> bool:
    """True for an `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` test."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.records: "list[ImportRecord]" = []
        self.has_dynamic_import = False

    def visit_If(self, node: "ast.If") -> None:
        if import_test_is_type_checking(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_Import(self, node: "ast.Import") -> None:
        for alias in node.names:
            self.records.append(ImportRecord(alias.name, (), 0, node.lineno))

    def visit_ImportFrom(self, node: "ast.ImportFrom") -> None:
        self.records.append(
            ImportRecord(
                node.module or "",
                tuple(alias.name for alias in node.names),
                node.level or 0,
                node.lineno,
            )
        )

    def visit_Call(self, node: "ast.Call") -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "__import__":
            self.has_dynamic_import = True
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if (func.value.id, func.attr) in _DYNAMIC_IMPORT_ATTR_CALLS:
                self.has_dynamic_import = True
        self.generic_visit(node)


def collect_imports(tree: "ast.AST") -> "tuple[list[ImportRecord], bool]":
    """Return `(records, has_dynamic_import)` for a parsed module.

    Imports in the true branch of a `TYPE_CHECKING` guard are skipped; its
    `else` branch is walked. `has_dynamic_import` flags `__import__`,
    `importlib.import_module`, `runpy.run_module` and `runpy.run_path` calls,
    which cannot be resolved statically.
    """
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.records, collector.has_dynamic_import


def resolve_relative_module(
    imported: str, level: int, current_module: Optional[str], current_is_package: bool
) -> Optional[str]:
    """Absolute dotted name for a relative import of `level` >= 1 found in
    `current_module`. `None` when `current_module` is unknown or the dots climb
    past the root."""
    if current_module is None:
        return None
    parts = current_module.split(".")
    if not current_is_package:
        parts = parts[:-1]
    climb = level - 1
    if climb > 0:
        if climb > len(parts):
            return None
        parts = parts[: len(parts) - climb]
    if imported:
        parts = parts + imported.split(".")
    return ".".join(parts) if parts else None


def module_name_for_file(rel_path: str) -> Optional[str]:
    """Dotted module name for a repo-relative `.py` path (backslashes
    normalised; `pkg/__init__.py` names `pkg`). `None` for a non-`.py` path."""
    rel_posix = rel_path.replace("\\", "/")
    if not rel_posix.endswith(".py"):
        return None
    parts = rel_posix.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    parts = [p for p in parts if p]
    return ".".join(parts) if parts else None
