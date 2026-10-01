"""Static registration-quad oracle: judges a commit's post-commit bytes without importing
the live registration tables.

`registration_violations(read_source, commit_paths)` reads the candidate `.py` files and the
five surface files through the injected `read_source` (repo-relative path -> bytes or None),
AST-extracts the literal tables, and feeds them to `registration_quad.check_registration_quad`
and `filter_known_violations`. Every rule stays in `registration_quad.py`; this module only
extracts. It never imports `OP_CLASSIFICATION`, `_OP_KEY_SCOPE`, `OP_MODULE_MAP`,
`_EAGER_OP_MODULES` or either debt ledger, so the verdict is a function of the bytes handed in.

Zero subprocess spawns; the caller owns how `read_source` obtains bytes.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Callable, Iterable, Optional

from coordinator_core.authz.registration_quad import (
    _SURFACE_FILES,
    QuadViolation,
    check_registration_quad,
    filter_known_violations,
)

_LEDGER_FILE = "coordinator_core/authz/registration_quad.py"
_SURFACE_PATHS = frozenset(_SURFACE_FILES.values())
_ALL_SURFACE_FILES = (*_SURFACE_FILES.values(), _LEDGER_FILE)


@dataclasses.dataclass(frozen=True)
class StaticQuadVerdict:
    """outcome is "skip" (nothing to judge), "pass", or "refuse"; `violations` is empty on
    a refuse caused by an unreadable or ambiguous surface, with `reason` naming the cause."""

    outcome: str
    violations: tuple[QuadViolation, ...] = ()
    reason: str = ""


class _ExtractError(Exception):
    pass


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _register_op_key_re():
    # Deferred: commit_tripwires pulls the git/bash-guard import graph this oracle must not
    # pay at import time.
    from coordinator_core.bash_guards.commit_tripwires import _REGISTER_OP_KEY_RE

    return _REGISTER_OP_KEY_RE


def _op_keys(read_source: Callable[[str], Optional[bytes]], candidates: Iterable[str]) -> set[str]:
    pattern = _register_op_key_re()
    keys: set[str] = set()
    for path in candidates:
        data = read_source(path)
        if not data or b"register_op(" not in data:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            m = pattern.match(line.strip())
            if m:
                keys.add(m.group(1))
    return keys


def _binding(tree: ast.Module, name: str, path: str) -> ast.expr:
    found: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                found.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
                found.append(node.value)
    if not found:
        raise _ExtractError(f"{name} is not bound in {path}")
    if len(found) > 1:
        raise _ExtractError(f"{name} is bound more than once in {path}")
    return found[0]


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _str_const(node: ast.expr, what: str, path: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise _ExtractError(f"{what} in {path} is not a string literal")


def _dict_items(node: ast.expr, name: str, path: str) -> list[tuple[str, ast.expr]]:
    if isinstance(node, ast.Call) and _call_name(node) == "MappingProxyType" and len(node.args) == 1:
        node = node.args[0]
    if not isinstance(node, ast.Dict):
        raise _ExtractError(f"{name} in {path} is not a dict literal")
    items = []
    for key, value in zip(node.keys, node.values):
        if key is None:
            raise _ExtractError(f"{name} in {path} uses ** unpacking")
        items.append((_str_const(key, f"{name} key", path), value))
    return items


def _str_collection(node: ast.expr, name: str, path: str) -> list[str]:
    if isinstance(node, ast.Call) and _call_name(node) == "frozenset":
        if not node.args:
            return []
        if len(node.args) != 1:
            raise _ExtractError(f"{name} in {path} is not a frozenset literal")
        node = node.args[0]
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        raise _ExtractError(f"{name} in {path} is not a collection literal")
    return [_str_const(e, f"{name} element", path) for e in node.elts]


def _extract(sources: dict[str, bytes]) -> dict:
    trees: dict[str, ast.Module] = {}
    for path, data in sources.items():
        try:
            trees[path] = ast.parse(data)
        except (SyntaxError, ValueError) as exc:
            raise _ExtractError(f"{path} does not parse: {exc}") from exc

    cls_path = _SURFACE_FILES["OP_CLASSIFICATION"]
    scope_path = _SURFACE_FILES["_OP_KEY_SCOPE"]
    map_path = _SURFACE_FILES["OP_MODULE_MAP"]
    eager_path = _SURFACE_FILES["_EAGER_OP_MODULES"]

    classification = {
        k: None for k, _ in _dict_items(_binding(trees[cls_path], "OP_CLASSIFICATION", cls_path), "OP_CLASSIFICATION", cls_path)
    }
    scope = {
        k: None for k, _ in _dict_items(_binding(trees[scope_path], "_OP_KEY_SCOPE", scope_path), "_OP_KEY_SCOPE", scope_path)
    }
    module_map = {
        k: _str_const(v, f"OP_MODULE_MAP[{k!r}]", map_path)
        for k, v in _dict_items(_binding(trees[map_path], "OP_MODULE_MAP", map_path), "OP_MODULE_MAP", map_path)
    }

    eager_node = _binding(trees[eager_path], "_EAGER_OP_MODULES", eager_path)
    if not isinstance(eager_node, (ast.List, ast.Tuple)):
        raise _ExtractError(f"_EAGER_OP_MODULES in {eager_path} is not a list literal")
    eager: set[str] = set()
    for elt in eager_node.elts:
        if not isinstance(elt, ast.Tuple) or not elt.elts:
            raise _ExtractError(f"_EAGER_OP_MODULES in {eager_path} holds a non-tuple element")
        eager.add(_str_const(elt.elts[0], "_EAGER_OP_MODULES module path", eager_path))

    debt = frozenset(
        _str_collection(
            _binding(trees[_LEDGER_FILE], "_KNOWN_UNCLASSIFIED_OPS_DEBT", _LEDGER_FILE),
            "_KNOWN_UNCLASSIFIED_OPS_DEBT",
            _LEDGER_FILE,
        )
    )
    incomplete = {
        k: tuple(_str_collection(v, f"_KNOWN_INCOMPLETE_REGISTRATIONS[{k!r}]", _LEDGER_FILE))
        for k, v in _dict_items(
            _binding(trees[_LEDGER_FILE], "_KNOWN_INCOMPLETE_REGISTRATIONS", _LEDGER_FILE),
            "_KNOWN_INCOMPLETE_REGISTRATIONS",
            _LEDGER_FILE,
        )
    }
    return {
        "classification": classification,
        "scope": scope,
        "module_map": module_map,
        "eager_modules": frozenset(eager),
        "debt": debt,
        "incomplete": incomplete,
    }


def _render(violations: list[QuadViolation]) -> str:
    return "; ".join(
        f"{v.op_key}: " + ", ".join(f"{s} in {p}" for s, p in v.missing_surface_files)
        for v in violations
    )


def registration_violations(
    read_source: Callable[[str], Optional[bytes]], commit_paths: Iterable[str]
) -> StaticQuadVerdict:
    """Judge the registration surfaces of the tree `read_source` describes.

    `commit_paths` selects whether anything needs judging; `read_source` supplies the
    post-commit bytes (None for an absent path).
    """
    paths = [_norm(p) for p in commit_paths]
    surface_in_paths = any(p in _SURFACE_PATHS for p in paths)
    candidates = [p for p in paths if p.startswith("coordinator_core/") and p.endswith(".py")]
    if not surface_in_paths and not candidates:
        return StaticQuadVerdict("skip", reason="no registration-relevant path in the commit")

    keys = _op_keys(read_source, candidates)
    if not keys and not surface_in_paths:
        return StaticQuadVerdict("skip", reason="no @register_op key in the commit")

    sources: dict[str, bytes] = {}
    absent: list[str] = []
    for path in _ALL_SURFACE_FILES:
        data = read_source(path)
        if data is None:
            absent.append(path)
        else:
            sources[path] = data
    if len(absent) == len(_ALL_SURFACE_FILES):
        return StaticQuadVerdict("skip", reason="no registration surface file in the tree")
    if absent:
        return StaticQuadVerdict("refuse", reason="registration surface file absent: " + ", ".join(absent))

    try:
        tables = _extract(sources)
    except _ExtractError as exc:
        return StaticQuadVerdict("refuse", reason=str(exc))

    raw = check_registration_quad(
        registry={k: None for k in keys},
        classification=tables["classification"],
        scope=tables["scope"],
        module_map=tables["module_map"],
        eager_modules=tables["eager_modules"],
    )
    kept = filter_known_violations(
        raw,
        classification_baseline=tables["debt"],
        incomplete_baseline=tables["incomplete"],
    )
    if kept:
        return StaticQuadVerdict("refuse", tuple(kept), _render(kept))
    return StaticQuadVerdict("pass")
