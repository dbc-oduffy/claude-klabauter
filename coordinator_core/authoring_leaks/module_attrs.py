"""Alias-binding and attribute-read scan: which `alias.name` reads in a .py file go through a first-party module import.

Pure: both functions take text or a parsed tree; no git, no file reads, no spawn.
`module_aliases` is a regex prefilter and never calls `ast.parse`, so a commit that reads no alias
pays no parse. `attribute_refs` is the AST confirmation.

Invariants and traps:
- An alias also bound by anything other than an import (assignment, def/class, parameter,
  for/with/except/comprehension target, global/nonlocal, match capture) yields no refs at all.
  Shadowing is excluded, never resolved.
- `import a.b` binds `a`, not `a.b`: `module_aliases` maps `a` to `{a, a.b}` and a read `a.b.f`
  resolves to module `a.b`, attribute `f` (longest imported dotted prefix, one attribute hop).
- `from p import m` binds `m` to the candidate module `p.m`; whether `p.m` is a module file is the
  caller's question.
- The true branch of an `if TYPE_CHECKING:` binds nothing; its `else` does.
"""

from __future__ import annotations

import ast
import re
from typing import NamedTuple

from coordinator_core.pyimports import (
    import_test_is_type_checking,
    module_name_for_file,
    resolve_relative_module,
)


class AttrRef(NamedTuple):
    """One `alias.name` read: `alias` is the local name, `module` the absolute dotted module it is
    bound to, `name` the attribute read, `lineno` the 1-based line of the first such read."""

    alias: str
    module: str
    name: str
    lineno: int


_IMPORT_STMT = re.compile(r"^(\s*)import\s+(.+)$")
_FROM_STMT = re.compile(r"^(\s*)from\s+(\.*)([\w.]*)\s+import\s*(.*)$")
_TC_IF = re.compile(r"^(\s*)if\s+(?:[\w.]*\.)?TYPE_CHECKING\s*:")
_AS_ITEM = re.compile(r"^([\w.]+)(?:\s+as\s+(\w+))?$")


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].rstrip()


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _from_target(rel: str, level: int, module: str) -> "str | None":
    if level == 0:
        return module or None
    current = module_name_for_file(rel)
    is_package = rel.replace("\\", "/").endswith("__init__.py")
    return resolve_relative_module(module, level, current, is_package)


def module_aliases(text: str, rel: str) -> "dict[str, frozenset[str]]":
    """Regex scan, no ast.parse: {local name: absolute dotted modules an import in `text` may bind it to}."""
    found: "dict[str, set[str]]" = {}
    lines = text.splitlines()
    tc_indent: "int | None" = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        code = _strip_comment(raw)
        if not code.strip():
            continue
        indent = _indent_of(code)
        if tc_indent is not None:
            if indent > tc_indent:
                continue
            tc_indent = None
        tc = _TC_IF.match(code)
        if tc:
            tc_indent = len(tc.group(1))
            continue
        m = _IMPORT_STMT.match(code)
        if m:
            body = m.group(2)
            while body.endswith("\\") and i < len(lines):
                body = body[:-1] + " " + _strip_comment(lines[i])
                i += 1
            for item in body.split(","):
                am = _AS_ITEM.match(item.strip())
                if not am:
                    continue
                dotted, alias = am.group(1), am.group(2)
                if alias:
                    found.setdefault(alias, set()).add(dotted)
                else:
                    root = dotted.split(".", 1)[0]
                    bucket = found.setdefault(root, set())
                    bucket.add(root)
                    bucket.add(dotted)
            continue
        f = _FROM_STMT.match(code)
        if f:
            dots, module, rest = f.group(2), f.group(3), f.group(4)
            while (rest.lstrip().startswith("(") and ")" not in rest) or rest.endswith("\\"):
                if i >= len(lines):
                    break
                rest = (rest[:-1] if rest.endswith("\\") else rest) + " " + _strip_comment(lines[i])
                i += 1
            rest = rest.replace("(", " ").replace(")", " ")
            base = _from_target(rel, len(dots), module)
            if base is None:
                continue
            for item in rest.split(","):
                am = _AS_ITEM.match(item.strip())
                if not am or am.group(1) == "*" or "." in am.group(1):
                    continue
                local = am.group(2) or am.group(1)
                found.setdefault(local, set()).add(f"{base}.{am.group(1)}")
    return {alias: frozenset(mods) for alias, mods in found.items()}


class _Bindings(ast.NodeVisitor):
    """Import bindings (TYPE_CHECKING true branch skipped) and every non-import binding name."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.direct: "dict[str, set[str]]" = {}
        self.plain: "dict[str, set[str]]" = {}
        self.other: "set[str]" = set()
        self._tc = 0

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        if import_test_is_type_checking(node.test):
            self._tc += 1
            for stmt in node.body:
                self.visit(stmt)
            self._tc -= 1
        else:
            for stmt in node.body:
                self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        if self._tc:
            return
        for a in node.names:
            if a.asname:
                self.direct.setdefault(a.asname, set()).add(a.name)
            else:
                root = a.name.split(".", 1)[0]
                bucket = self.plain.setdefault(root, {root})
                bucket.add(a.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._tc:
            return
        base = _from_target(self.rel, node.level or 0, node.module or "")
        for a in node.names:
            local = a.asname or a.name
            if a.name == "*" or base is None:
                continue
            self.direct.setdefault(local, set()).add(f"{base}.{a.name}")

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            self.other.add(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self.other.add(node.arg)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.other.add(node.name)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.other.add(node.name)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.other.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.other.update(node.names)

    visit_Nonlocal = visit_Global  # type: ignore[assignment]

    def visit_MatchAs(self, node: "ast.MatchAs") -> None:
        if node.name:
            self.other.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: "ast.MatchStar") -> None:
        if node.name:
            self.other.add(node.name)

    def visit_MatchMapping(self, node: "ast.MatchMapping") -> None:
        if node.rest:
            self.other.add(node.rest)
        self.generic_visit(node)


def _chain(node: ast.expr) -> "list[str] | None":
    parts: "list[str]" = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return parts


class _Reads(ast.NodeVisitor):
    def __init__(self, bindings: _Bindings) -> None:
        self.b = bindings
        self.refs: "dict[tuple[str, str, str], int]" = {}

    def _add(self, alias: str, module: str, name: str, lineno: int) -> None:
        self.refs.setdefault((alias, module, name), lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = _chain(node) if isinstance(node.ctx, ast.Load) else None
        if parts is None or len(parts) < 2:
            self.generic_visit(node)
            return
        root = parts[0]
        if root in self.b.other:
            return
        for module in self.b.direct.get(root, ()):
            self._add(root, module, parts[1], node.lineno)
        imported = self.b.plain.get(root)
        if imported:
            k = 0
            for j in range(len(parts) - 1):
                if ".".join(parts[: j + 1]) in imported:
                    k = j
            self._add(root, ".".join(parts[: k + 1]), parts[k + 1], node.lineno)


def attribute_refs(tree: ast.AST, rel: str) -> "list[AttrRef]":
    """Load-context `alias.name` reads whose alias an import in `tree` binds to a dotted module."""
    bindings = _Bindings(rel)
    bindings.visit(tree)
    if not (bindings.direct or bindings.plain):
        return []
    reads = _Reads(bindings)
    reads.visit(tree)
    out = [AttrRef(a, m, n, ln) for (a, m, n), ln in reads.refs.items()]
    out.sort(key=lambda r: r.lineno)
    return out
