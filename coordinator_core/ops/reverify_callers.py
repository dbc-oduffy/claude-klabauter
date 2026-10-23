"""Pure removed-symbol and committed-caller checker for review.reverify revert mode.

`removed_symbols` diffs the top-level names two module byte strings bind;
`callers_of` finds which sources use a given module's names. Stdlib `ast` plus
`coordinator_core.pyimports` only: no git, no filesystem, no spawn.
"""

from __future__ import annotations

import ast
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from coordinator_core.pyimports import module_name_for_file, resolve_relative_module

_PARSE_ERRORS = (SyntaxError, ValueError, RecursionError, MemoryError)


def _parse(source: bytes) -> Optional[ast.Module]:
    try:
        return ast.parse(source)
    except _PARSE_ERRORS:
        return None


def _target_names(target: ast.AST, names: Set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _target_names(elt, names)
    elif isinstance(target, ast.Starred):
        _target_names(target.value, names)


def _collect_names(body: List[ast.stmt], names: Set[str]) -> None:
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                _target_names(target, names)
        elif isinstance(stmt, ast.AnnAssign):
            _target_names(stmt.target, names)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.If):
            _collect_names(stmt.body, names)
            _collect_names(stmt.orelse, names)
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            _collect_names(stmt.body, names)
            for handler in stmt.handlers:
                _collect_names(handler.body, names)
            _collect_names(stmt.orelse, names)
            _collect_names(stmt.finalbody, names)


def _top_level_names(tree: ast.Module) -> Set[str]:
    """Names bound at module level: defs, assignments (tuple targets too),
    imports, and the same inside top-level if/try bodies."""
    names: Set[str] = set()
    _collect_names(tree.body, names)
    return names


def removed_symbols(before: bytes, after: bytes) -> FrozenSet[str]:
    """Top-level names `before` binds and `after` does not.

    Unparseable `after` yields every top-level name of `before`; unparseable
    `before` yields the empty set.
    """
    before_tree = _parse(before)
    if before_tree is None:
        return frozenset()
    before_names = _top_level_names(before_tree)
    after_tree = _parse(after)
    if after_tree is None:
        return frozenset(before_names)
    return frozenset(before_names - _top_level_names(after_tree))


def _dotted(node: ast.AST) -> Optional[str]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _used_names(tree: ast.Module, module: str, wanted: Set[str], caller: str) -> Set[str]:
    current = module_name_for_file(caller)
    is_package = caller.replace("\\", "/").endswith("__init__.py")
    module_aliases: Dict[str, str] = {}
    name_aliases: Dict[str, Tuple[str, str]] = {}
    hits: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = resolve_relative_module(node.module or "", node.level, current, is_package)
            else:
                base = node.module
            if not base:
                continue
            for alias in node.names:
                if alias.name == "*":
                    if base == module:
                        hits.update(wanted)
                    continue
                local = alias.asname or alias.name
                module_aliases[local] = base + "." + alias.name
                name_aliases[local] = (base, alias.name)
                if base == module and alias.name in wanted:
                    hits.add(alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            bound = name_aliases.get(node.id)
            if bound and bound[0] == module and bound[1] in wanted:
                hits.add(bound[1])
        elif isinstance(node, ast.Attribute) and node.attr in wanted:
            prefix = _dotted(node.value)
            if prefix is not None and module_aliases.get(prefix) == module:
                hits.add(node.attr)
    return hits


def callers_of(
    module: str, names: Iterable[str], sources: Mapping[str, bytes]
) -> List[dict]:
    """Sorted `{"caller": path, "symbol": name}` rows for each source using one
    of `names` from `module`.

    Detects `from module import name [as a]` and attribute access
    `<alias>.name`, where the alias comes from `import module [as a]` or
    `from <package> import <leaf> [as a]`, at module level or function-local.
    Relative imports resolve against the caller's own path. An unparseable
    source is skipped.
    """
    wanted = set(names)
    if not wanted:
        return []
    rows: List[dict] = []
    for caller, source in sources.items():
        tree = _parse(source)
        if tree is None:
            continue
        for symbol in _used_names(tree, module, wanted, caller):
            rows.append({"caller": caller, "symbol": symbol})
    rows.sort(key=lambda r: (r["caller"], r["symbol"]))
    return rows
