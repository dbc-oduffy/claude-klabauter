"""
coordinator_core.cartography.symbols — per-file AST symbol table extraction.

Purpose: pure-function producer of a per-Python-file symbol table (module
docstring, module-level constants with their literal values, top-level
classes with their methods, and top-level functions — each carrying its
signature and docstring) via the stdlib ``ast`` module. Replaces an agentic
full-file-read inventory pass with a cheap, deterministic static walk.

Emission shape mirrors ``coordinator_core/ops/roadmap_dag.py``'s
``{nodes, edges}`` convention loosely: this module emits ``{"files": [...]}``
where each file entry is a self-contained symbol table for one ``*.py`` file.

Negative-spec:
  - Does NOT execute or import any target file — pure ``ast.parse`` static
    analysis. A file that fails to parse (``SyntaxError``) is recorded with
    an ``"error"`` field, not raised out of the walk.
  - Does NOT resolve dynamic dispatch, decorators' runtime effects, or
    cross-file symbol references — see ``cartography.edges`` for the
    (also static-only) import/call graph, which shares this same limitation.
  - Does NOT record local (function-nested) symbols — only module-level
    constants/classes/functions and class-level methods.
  - Only module-level constants whose value is a literal (``ast.literal_eval``-
    safe) are recorded with a ``value`` field; non-literal assignments (e.g.
    ``X = some_call()``) are recorded with ``value: null`` — never partially
    evaluated or executed.
  - Does NOT let one file's data fail the batch: a constant NAME is always kept,
    and a value that cannot survive JSON encoding is normalized (``_json_safe``)
    or, failing that, elided with ``value_elided: true`` plus a per-file
    ``"error"`` naming the path (``_serializable_entry``).

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md
§ chunk C4 (cartography.symbols).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.cartography._guard import PathEscapeError, path_guard


def _signature(node: ast.AST) -> str:
    """Render a function/method signature as source-like text.

    Uses ``ast.unparse`` (stdlib, Python 3.9+) against a copy of the node
    with its body stripped to a single ``pass``, so only the ``def name(...)
    -> ret:`` header is rendered — never the body.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = ast.unparse(node.args)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        return f"{prefix} {node.name}({args}){returns}"
    return ""


def _json_safe(value: Any) -> Any:
    """Recursively normalize an ``ast.literal_eval`` result into a JSON-serializable shape.

    ``ast.literal_eval`` legitimately returns ``set``/``frozenset``/``bytes`` for literal
    set-display (``{"a", "b"}``) or bytes-literal (``b"..."``) RHS nodes — none of which
    ``json.dumps`` can encode. Sets/frozensets become sorted lists (stable, order-independent
    across runs since set iteration order is not semantically meaningful here); bytes become
    their ``repr()`` string (preserves the literal's content without pretending it's text).
    Dicts/lists/tuples are walked so a nested set (e.g. inside a dict value) is also caught.
    Dict KEYS are normalized too: ``json.dumps`` accepts only ``str``/``int``/``float``/
    ``bool``/``None`` keys, so a tuple-keyed literal mapping (a common table shape here —
    ``{("no-verify", "setsid_wrapper"): "..."}``) becomes its ``repr()`` string. Any
    remaining terminal that is not a JSON scalar (``complex``, ``Ellipsis``) likewise
    degrades to ``repr()`` rather than reaching the JSON-RPC envelope untranslated.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, bytes):
        return repr(value)
    if isinstance(value, dict):
        return {_json_safe_key(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _json_safe_key(key: Any) -> Any:
    """Normalize one mapping key into a type ``json.dumps`` accepts as an object key."""
    if key is None or isinstance(key, (str, int, float, bool)):
        return key
    return repr(key)


def _literal_value(node: ast.AST) -> Optional[Any]:
    """Return the literal value of an assignment RHS, or None if non-literal.

    Uses ``ast.literal_eval`` — never executes arbitrary code. Any node that
    is not a literal-evaluable expression (calls, names, comprehensions, etc.)
    yields ``None`` rather than raising or partially evaluating. The result is
    passed through ``_json_safe`` so a literal set/frozenset/bytes RHS (e.g.
    ``_TERMINAL_STATUSES = {"consumed", "superseded"}``) never reaches the
    JSON-RPC envelope as a non-serializable Python type.
    """
    try:
        return _json_safe(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _function_entry(node: ast.AST) -> Dict[str, Any]:
    return {
        "name": node.name,
        "signature": _signature(node),
        "docstring": ast.get_docstring(node),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "lineno": node.lineno,
    }


def _class_entry(node: ast.ClassDef) -> Dict[str, Any]:
    methods: List[Dict[str, Any]] = []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(_function_entry(item))
    bases = [ast.unparse(base) for base in node.bases]
    return {
        "name": node.name,
        "bases": bases,
        "docstring": ast.get_docstring(node),
        "methods": methods,
        "lineno": node.lineno,
    }


def _module_constants(tree: ast.Module) -> List[Dict[str, Any]]:
    """Extract module-level simple-name assignments (``NAME = <expr>``).

    Only top-level ``ast.Assign``/``ast.AnnAssign`` statements are recorded
    (function/class bodies are excluded — those are handled by
    ``_class_entry``/``_function_entry`` or skipped as local state).
    """
    constants: List[Dict[str, Any]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    constants.append(
                        {
                            "name": target.id,
                            "value": _literal_value(stmt.value),
                            "lineno": stmt.lineno,
                        }
                    )
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            constants.append(
                {
                    "name": stmt.target.id,
                    "value": _literal_value(stmt.value) if stmt.value is not None else None,
                    "lineno": stmt.lineno,
                }
            )
    return constants


def symbol_table_for_file(target_root: str | Path, file_path: str | Path) -> Dict[str, Any]:
    """Return the symbol table for a single Python file.

    Args:
        target_root: containment root — ``file_path`` must resolve under it
            (see ``cartography._guard.path_guard``).
        file_path: absolute or ``target_root``-relative path to a ``*.py`` file.

    Returns:
        {
            "path": <str, target_root-relative POSIX path>,
            "module_docstring": <str | None>,
            "constants": [{"name", "value", "lineno"}, ...],
            "classes": [{"name", "bases", "docstring", "methods", "lineno"}, ...],
            "functions": [{"name", "signature", "docstring", "is_async", "lineno"}, ...],
        }
        or, on a parse failure:
        {"path": <str>, "error": <str>}

    Never raises for a malformed target file — a ``SyntaxError`` (or any
    ``OSError`` reading the file) is captured into the ``"error"`` field.
    Raises ``PathEscapeError`` (propagated from ``path_guard``) if
    ``file_path`` escapes ``target_root`` — that is a containment violation,
    not a per-file data condition, and is never swallowed.
    """
    resolved = path_guard(target_root, file_path)
    rel_path = resolved.relative_to(Path(target_root).resolve()).as_posix()

    try:
        source = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return {"path": rel_path, "error": f"read error: {exc}"}

    try:
        tree = ast.parse(source, filename=str(resolved))
    except SyntaxError as exc:
        return {"path": rel_path, "error": f"SyntaxError: {exc}"}

    classes: List[Dict[str, Any]] = []
    functions: List[Dict[str, Any]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            classes.append(_class_entry(stmt))
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_function_entry(stmt))

    return {
        "path": rel_path,
        "module_docstring": ast.get_docstring(tree),
        "constants": _module_constants(tree),
        "classes": classes,
        "functions": functions,
    }


def build_symbols(target_root: str | Path, files: List[str | Path]) -> Dict[str, Any]:
    """Build the aggregate symbol-table payload for a list of Python files.

    Args:
        target_root: containment root (see ``symbol_table_for_file``).
        files: list of absolute or ``target_root``-relative ``*.py`` paths.

    Returns:
        {"files": [<symbol_table_for_file(...) entry>, ...]}

    Each file is processed independently — a parse error on one file never
    aborts the batch (recorded per-file via the ``"error"`` field), and neither
    does an entry that turns out not to be JSON-encodable (see
    ``_serializable_entry``). A containment violation is the one exception: it
    propagates, because it is a caller error about the batch, not file data.
    """
    return {"files": [_serializable_entry(target_root, f) for f in files]}


def _serializable_entry(target_root: str | Path, file_path: str | Path) -> Dict[str, Any]:
    """Return one file's symbol table, guaranteed JSON-encodable and path-attributed.

    ``_json_safe`` normalizes every value shape ``ast.literal_eval`` is known to
    produce, but the JSON-RPC envelope serializes the WHOLE batch at once: one
    entry the normalizer did not anticipate would otherwise fail the entire call
    with a type name and no file to bisect from. Any such entry degrades here to
    its constant NAMES with elided values, keeping the offending path in the reply.

    Negative-spec:
      - Does NOT swallow ``PathEscapeError`` — a path escaping ``target_root`` is a
        containment violation about the request, not a per-file data condition.
      - Does NOT drop the file entry itself: the path is always reported, so a
        caller can always name what degraded.
    """
    try:
        entry = symbol_table_for_file(target_root, file_path)
    except PathEscapeError:
        raise
    except Exception as exc:
        return {"path": str(file_path), "error": f"{type(exc).__name__}: {exc}"}

    try:
        json.dumps(entry)
    except (TypeError, ValueError) as exc:
        for constant in entry.get("constants", []):
            try:
                json.dumps(constant.get("value"))
            except (TypeError, ValueError):
                constant["value"] = None
                constant["value_elided"] = True
        entry["error"] = f"non-serializable symbol data elided: {exc}"
        try:
            json.dumps(entry)
        except (TypeError, ValueError) as residual:
            return {
                "path": entry.get("path", str(file_path)),
                "error": f"non-serializable symbol table dropped: {residual}",
            }
    return entry
