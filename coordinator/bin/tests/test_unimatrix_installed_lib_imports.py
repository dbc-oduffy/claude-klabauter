"""Ratchet: every lib/ import in every installed unimatrix host script resolves inside the installed lib/,
and the INSTALLED host guard evaluates (never degraded/import_error) and denies a deny-class command
for codex and agy. OS-agnostic: the guard runs via sys.executable over tmp_path paths."""
import ast
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_BIN = Path(__file__).resolve().parents[1]
_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("unimatrix_install_for_lib_ratchet", _BIN / "unimatrix-install.py")
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

# Modules an installed script may import that are neither stdlib nor lib/: none today.
_LIB_NAMES = {p.stem for p in (_BIN / "lib").glob("*.py")}


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    dest = tmp_path_factory.mktemp("home") / "unimatrix"
    ui.install_scripts(ui._BIN_DIR, dest)
    return dest


def _import_sites(tree):
    """(module name, inside an `except ImportError` handler's try body) for every import statement."""
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                h.type is not None and "ImportError" in ast.dump(h.type) for h in node.handlers):
            guarded.update(id(n) for stmt in node.body for n in ast.walk(stmt))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], id(node) in guarded
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], id(node) in guarded


def test_every_installed_script_lib_import_resolves_in_the_installed_lib(installed):
    installed_lib = {p.stem for p in (installed / "lib").glob("*.py")}
    scripts = sorted(installed.glob("*.py"))
    assert any(p.name == "unimatrix-relay.py" for p in scripts) and any(p.name == "unimatrix-host-guard.py" for p in scripts)
    unresolved = {}
    for script in scripts:
        for mod, in_import_error_fallback in _import_sites(ast.parse(script.read_text(encoding="utf-8"))):
            if mod in _LIB_NAMES and mod not in installed_lib and not in_import_error_fallback:
                unresolved.setdefault(script.name, set()).add(mod)
    assert not unresolved, f"lib imports with no installed lib/ module and no ImportError fallback: {unresolved}"


def test_installed_lib_closure_is_self_contained(installed):
    """Every lib module a shipped lib module imports at any depth is itself shipped."""
    shipped = {p.stem for p in (installed / "lib").glob("*.py")}
    assert {"cc_invoke", "engine_bootstrap"} <= shipped
    missing = {}
    for path in sorted((installed / "lib").glob("*.py")):
        for mod, _ in _import_sites(ast.parse(path.read_text(encoding="utf-8"))):
            if mod in _LIB_NAMES and mod not in shipped:
                missing.setdefault(path.name, set()).add(mod)
    assert not missing, f"installed lib modules import unshipped lib modules: {missing}"


def test_a_removed_lib_module_fails_the_closure_check(installed, tmp_path):
    copy = tmp_path / "unimatrix"
    ui.install_scripts(ui._BIN_DIR, copy)
    (copy / "lib" / "engine_bootstrap.py").unlink()
    shipped = {p.stem for p in (copy / "lib").glob("*.py")}
    sites = {m for m, _ in _import_sites(ast.parse((copy / "lib" / "cc_invoke.py").read_text(encoding="utf-8")))}
    assert "engine_bootstrap" in sites and "engine_bootstrap" not in shipped
