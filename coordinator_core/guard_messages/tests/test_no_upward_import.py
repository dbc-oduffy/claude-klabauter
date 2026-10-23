import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
_NON_TEST_IMPORTERS_OF_TESTING = "guard_messages.testing"


def _modules(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_guard_messages_imports_nothing_from_bash_guards():
    for py in _PKG.rglob("*.py"):
        for mod in _modules(py):
            assert "bash_guards" not in mod, (py, mod)


def test_package_modules_do_not_import_testing_adapter():
    for py in _PKG.glob("*.py"):
        if py.name == "testing.py":
            continue
        for mod in _modules(py):
            assert not mod.endswith(_NON_TEST_IMPORTERS_OF_TESTING), (py, mod)
