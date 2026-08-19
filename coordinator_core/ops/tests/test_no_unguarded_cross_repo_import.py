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
    - the import sits inside a `try:` block with an `ImportError` fallback, or
    - the import sits inside a function/method body.

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


def _unguarded_module_scope_imports(tree: ast.Module) -> List[int]:
    """Line numbers of `coordinator[.*]` imports reachable at module scope.

    Descends through module-scope control flow (`if`/`with`) but stops at
    `Try` (guarded) and at function/class bodies (deferred) — both are the
    sanctioned shapes.
    """
    offending: List[int] = []

    def walk(body: List[ast.stmt]) -> None:
        for node in body:
            if isinstance(
                node, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if _names_coordinator_pkg(node):
                offending.append(node.lineno)
            elif isinstance(node, (ast.If, ast.With, ast.AsyncWith)):
                walk(node.body)
                walk(getattr(node, "orelse", []))

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
