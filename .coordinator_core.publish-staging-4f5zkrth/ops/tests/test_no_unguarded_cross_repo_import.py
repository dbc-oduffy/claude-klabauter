"""No production engine module imports the `coordinator` namespace package
unguarded at module scope.

Why this is a gate and not a convention: `coordinator/` (the DoE-claude plugin
tree's `bin/lib`, vendored alongside this engine) carries no `__init__.py`, so
`import coordinator.bin.lib.win_argv` resolves ONLY while the claude-klabauter root
happens to be on `sys.path`. Callers such as `coordinator/bin/lib/cc_invoke.py`
insert that root and pop it again around their own imports, so an engine module
imported after that window — for example by
`coordinator_core.ops._eager_import_all()` — sees no `coordinator` package at
all and raises `ModuleNotFoundError`.

The observable failure that motivated this (2026-08-19): a module-level
`from coordinator.bin.lib.win_argv import ...` in
`coordinator_core/ops/_app_session_runtime.py` poisoned the whole
`coordinator_core.ops.app_session` module — including its OWN carefully guarded
bootstrap of the same symbol, which sat a few lines below the import of the
poisoned module and was therefore never reached.

Permitted shapes (both defer resolution to a point where a fallback can run):
    - the import sits inside a `try:` block where at least one `except` clause
      catches `ImportError` (or a superclass — `Exception`/`BaseException`, or
      a bare `except:`), or
    - the import sits inside a function/method body.

A `try:` whose handlers only catch unrelated exceptions (or a bare
`try/finally` with no `except` at all) does NOT count as guarded — the
walker descends into its body and flags any `coordinator[.*]` import found
there, since `ImportError` would propagate uncaught.

Negative-spec:
    - Does NOT ban the dependency itself. Reaching into `coordinator/bin/lib`
      for the shared spawn substrate is sanctioned; only the UNGUARDED
      module-scope form is.
    - Does NOT police test modules. A test that fails to import its own
      fixtures is loud and local; a poisoned production op module is silent
      until an op is dispatched.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

_ENGINE_ROOT = Path(__file__).resolve().parents[2]

#: Directory/file name fragments marking a module as test-only.
_TEST_MARKERS = ("tests", "conftest.py")


def _is_production_module(path: Path) -> bool:
    parts = path.relative_to(_ENGINE_ROOT).parts
    if any(p in _TEST_MARKERS for p in parts):
        return False
    return not path.name.startswith("test_")


def _names_coordinator_pkg(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return module == "coordinator" or module.startswith("coordinator.")
    if isinstance(node, ast.Import):
        return any(
            alias.name == "coordinator" or alias.name.startswith("coordinator.")
            for alias in node.names
        )
    return False


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    """True if `handler` would catch an `ImportError` — including a bare
    `except:` and a superclass catch (`Exception`/`BaseException`)."""
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        _dotted_name(t) in ("ImportError", "Exception", "BaseException") for t in types
    )


def _try_is_guarded(node: ast.Try) -> bool:
    return any(_handler_catches_import_error(h) for h in node.handlers)


def _unguarded_module_scope_imports(tree: ast.Module) -> List[int]:
    """Line numbers of `coordinator[.*]` imports reachable at module scope.

    Descends through module-scope control flow (`if`/`with`/`for`/`while`/
    `match`) and stops at function/class bodies (deferred — a sanctioned
    shape). A `Try` only counts as guarded, and is skipped, when at least one
    of its handlers would catch `ImportError`; otherwise the walker descends
    into the try body, since an import there is not actually protected.
    """
    offending: List[int] = []

    def walk(body: List[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Try):
                if not _try_is_guarded(node):
                    walk(node.body)
                continue
            if _names_coordinator_pkg(node):
                offending.append(node.lineno)
            elif isinstance(
                node,
                (ast.If, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While),
            ):
                walk(node.body)
                walk(getattr(node, "orelse", []))
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    walk(case.body)

    walk(tree.body)
    return offending


def test_no_unguarded_coordinator_package_import_at_module_scope() -> None:
    violations: List[str] = []
    for path in sorted(_ENGINE_ROOT.rglob("*.py")):
        if not _is_production_module(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a broken file is another gate's job
            continue
        for lineno in _unguarded_module_scope_imports(tree):
            violations.append(f"{path.relative_to(_ENGINE_ROOT).as_posix()}:{lineno}")

    assert not violations, (
        "module-scope import of the `coordinator` namespace package with no "
        "ImportError fallback — resolves only while the claude-klabauter root is on "
        "sys.path, and poisons every op registered by the importing module "
        "when it is not:\n  " + "\n  ".join(violations)
    )


def _flag(source: str) -> List[int]:
    return _unguarded_module_scope_imports(ast.parse(source))


def test_bare_module_scope_import_is_flagged() -> None:
    lines = _flag("from coordinator.bin.lib.win_argv import x\n")
    assert lines == [1]


def test_import_nested_in_module_scope_for_is_flagged() -> None:
    lines = _flag(
        "for _ in range(1):\n"
        "    from coordinator.bin.lib.win_argv import x\n"
    )
    assert lines == [2]


def test_import_nested_in_while_is_flagged() -> None:
    lines = _flag(
        "while True:\n"
        "    from coordinator.bin.lib.win_argv import x\n"
        "    break\n"
    )
    assert lines == [2]


def test_import_nested_in_match_case_is_flagged() -> None:
    lines = _flag(
        "match 1:\n"
        "    case 1:\n"
        "        from coordinator.bin.lib.win_argv import x\n"
    )
    assert lines == [3]


def test_import_nested_in_module_scope_if_body_is_flagged() -> None:
    lines = _flag(
        "if True:\n"
        "    from coordinator.bin.lib.win_argv import x\n"
    )
    assert lines == [2]


def test_import_nested_in_module_scope_if_orelse_is_flagged() -> None:
    lines = _flag(
        "if True:\n"
        "    pass\n"
        "else:\n"
        "    from coordinator.bin.lib.win_argv import x\n"
    )
    assert lines == [4]


def test_import_guarded_by_try_except_import_error_is_not_flagged() -> None:
    lines = _flag(
        "try:\n"
        "    from coordinator.bin.lib.win_argv import x\n"
        "except ImportError:\n"
        "    pass\n"
    )
    assert lines == []


def test_import_inside_function_body_is_not_flagged() -> None:
    lines = _flag(
        "def f():\n"
        "    from coordinator.bin.lib.win_argv import x\n"
    )
    assert lines == []


def test_import_inside_class_body_is_not_flagged() -> None:
    lines = _flag(
        "class C:\n"
        "    from coordinator.bin.lib.win_argv import x\n"
    )
    assert lines == []


def test_unrelated_import_is_not_flagged() -> None:
    lines = _flag("from coordinator_core.x import y\n")
    assert lines == []


def test_try_except_mismatched_exception_type_is_flagged() -> None:
    lines = _flag(
        "try:\n"
        "    from coordinator.bin.lib.win_argv import x\n"
        "except ValueError:\n"
        "    pass\n"
    )
    assert lines == [2]


def test_bare_try_finally_with_no_except_is_flagged() -> None:
    lines = _flag(
        "try:\n"
        "    from coordinator.bin.lib.win_argv import x\n"
        "finally:\n"
        "    pass\n"
    )
    assert lines == [2]
