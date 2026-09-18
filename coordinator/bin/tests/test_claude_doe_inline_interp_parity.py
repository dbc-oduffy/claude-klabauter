"""
Parity pin: `claude-doe.py`'s inline interpreter-resolution ladder must stay a
byte-for-byte (modulo the private-name prefix) mirror of
`coordinator/bin/lib/python_interp.py`.

Purpose: `claude-doe.py` ships STANDALONE (see its `_machine_local_argv`
docstring) and cannot import a sibling `lib/` module, so it carries its own
copy of `is_console_python_basename` / `resolve_console_python` inline. A
second hand-maintained copy of a security-relevant ladder is normally a
defect-in-waiting -- this test is what promotes it from "duplicate that will
drift" to "duplicate that is pinned and re-verified every run": it extracts
both functions' bodies with `ast`, normalizes away the one permitted
difference (the inline copy's leading-underscore names), and fails with the
diff the moment they diverge.

Negative spec: never a text diff over raw source (the leading-underscore
rename would show as noise on every line); never a `subprocess` import-and-run
comparison (this is a pure `ast` structural comparison, no spawn).

Spec: docs/plans/2026-08-31-the-sys-executable-class-one-shared-inte.md, C6.
"""
from __future__ import annotations

import ast
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parents[1]
CLAUDE_DOE_PATH = BIN_DIR / "claude-doe.py"
PYTHON_INTERP_PATH = BIN_DIR / "lib" / "python_interp.py"

# (inline name in claude-doe.py, shared name in python_interp.py)
PAIRED_FUNCTIONS = [
    ("_is_console_python_basename", "is_console_python_basename"),
    ("_resolve_console_python", "resolve_console_python"),
]


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


_INLINE_NAMES = {inline for inline, _shared in PAIRED_FUNCTIONS}


class _NameNormalizer(ast.NodeTransformer):
    """Strip the inline copy's leading underscore from function defs and calls.

    The inline copy's function names -- both the `def` itself and any call
    from one paired function to the other (`_resolve_console_python` calls
    `_is_console_python_basename`) -- carry a leading underscore the
    shared-module copy does not. Everything else -- docstring, body,
    argument names -- must match exactly.
    """

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        if node.name in _INLINE_NAMES:
            node.name = node.name.lstrip("_")
        self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id in _INLINE_NAMES:
            node.id = node.id.lstrip("_")
        return node


def _normalized_body_dump(func: ast.FunctionDef, own_name: str) -> str:
    normalizer = _NameNormalizer()
    normalizer.visit(func)
    return ast.dump(func, annotate_fields=True, include_attributes=False)


def test_claude_doe_inline_ladder_matches_shared_resolver():
    claude_doe_tree = ast.parse(CLAUDE_DOE_PATH.read_text(encoding="utf-8"), filename=str(CLAUDE_DOE_PATH))
    python_interp_tree = ast.parse(PYTHON_INTERP_PATH.read_text(encoding="utf-8"), filename=str(PYTHON_INTERP_PATH))

    for inline_name, shared_name in PAIRED_FUNCTIONS:
        inline_func = _find_function(claude_doe_tree, inline_name)
        shared_func = _find_function(python_interp_tree, shared_name)

        # Drop docstrings: the inline copy's docstring intentionally explains
        # the standalone-duplication constraint and cites this pin, which the
        # shared module's docstring does not -- prose divergence there is not
        # a ladder drift.
        inline_body = [
            stmt for stmt in inline_func.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str))
        ]
        shared_body = [
            stmt for stmt in shared_func.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str))
        ]
        inline_func.body = inline_body
        shared_func.body = shared_body

        inline_dump = _normalized_body_dump(inline_func, inline_name)
        shared_dump = _normalized_body_dump(shared_func, shared_name)

        assert inline_dump == shared_dump, (
            f"claude-doe.py::{inline_name} has drifted from "
            f"python_interp.py::{shared_name} -- these are pinned as an "
            f"intentional standalone duplicate (C6); re-sync the inline body "
            f"from the shared resolver.\n"
            f"inline:  {inline_dump}\n"
            f"shared:  {shared_dump}"
        )
