"""
Structural gate: coordinator_core.ops.dispatch_emit.pathspec and .emit must
never reference a tree-survey surface (AC3, AC9's negative spec).

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C3, § C5.

The staff review's answer to "is docstring + tests sufficient to hold the
negative spec?" was no: the tempting fix for an empty pathspec or an empty
terminal test scope is precisely the forbidden one (enumerate the tree,
glob a directory, shell out to git status), and the person reaching for it
will be editing pathspec.py with a failing assertion-level test in front of
them, not reading the module docstring. This gate walks pathspec.py's own
AST and fails on ANY reference to ``os.walk``, ``glob.glob``/``glob.iglob``,
``Path.glob``/``Path.rglob``/``Path.iterdir``, or any ``subprocess`` call
-- structurally, not by regex, so a docstring mentioning these names in
prose (as this module's own docstring does) cannot trip it.

C5 (dispatch_emit executor) extended this gate to cover ``emit.py`` too:
``emit.py`` has no tree-survey code path today, and the point of a
structural gate over a per-behaviour test/docstring is that it holds even
before such a code path is ever written -- covering only pathspec.py would
leave emit.py free to acquire one silently. Per the C5 dispatch brief, this
is an extension of the existing gate, not a second gate file.

Shape follows coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py
(AST-based, not regex, so comments/docstrings are structurally excluded).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DISPATCH_EMIT_DIR = _REPO_ROOT / "coordinator_core" / "ops" / "dispatch_emit"
_TARGET_MODULE = _DISPATCH_EMIT_DIR / "pathspec.py"
_EMIT_MODULE = _DISPATCH_EMIT_DIR / "emit.py"
_GUARDED_MODULES = (_TARGET_MODULE, _EMIT_MODULE)

_FORBIDDEN_ATTRS = {"walk", "glob", "iglob", "rglob", "iterdir"}
_SUBPROCESS_CALL_NAMES = {"run", "Popen", "check_output", "call", "check_call"}
# Review: code-reviewer (wsc-B) -- `from os import walk` (etc.) bypasses
# visit_ImportFrom's {"glob", "subprocess"} module check entirely, then
# the bare-name call it enables bypasses visit_Call's
# _SUBPROCESS_CALL_NAMES check too, since none of these names were in it.
_OS_FORBIDDEN_NAMES = {"walk", "listdir", "scandir", "system", "popen"}


class TreeSurveyVisitor(ast.NodeVisitor):
    """Collects every AST-level reference to a tree-survey surface."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []
        # Local names bound by `from os import X` for a forbidden X --
        # tracked so a later bare-name call site (`walk(...)`) is caught
        # even though it carries no `os.` prefix at the call site.
        self._os_imported_local_names: set[str] = set()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _FORBIDDEN_ATTRS:
            self.violations.append((node.lineno, f"attribute access .{node.attr}"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # Review: code-reviewer (wsc-B) -- `getattr(os, "walk")(path)`: the
        # outer call's `.func` is itself a Call (the `getattr(...)`), not
        # an Attribute/Name, so the name-resolution below would silently
        # fall through to None without this explicit getattr check.
        if (
            isinstance(func, ast.Call)
            and isinstance(func.func, ast.Name)
            and func.func.id == "getattr"
            and len(func.args) >= 2
        ):
            base = func.args[0]
            attr_arg = func.args[1]
            base_is_os = isinstance(base, ast.Name) and base.id == "os"
            if (
                base_is_os
                and isinstance(attr_arg, ast.Constant)
                and isinstance(attr_arg.value, str)
                and (attr_arg.value in _FORBIDDEN_ATTRS or attr_arg.value in _OS_FORBIDDEN_NAMES)
            ):
                self.violations.append(
                    (func.lineno, f"getattr(os, {attr_arg.value!r}) indirection")
                )
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name in _SUBPROCESS_CALL_NAMES:
            self.violations.append((node.lineno, f"call to {name}(...)"))
        if name is not None and name in self._os_imported_local_names:
            self.violations.append((node.lineno, f"call to {name}(...) (from os import)"))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in {"glob", "subprocess"}:
                self.violations.append((node.lineno, f"import {alias.name}"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module in {"glob", "subprocess"}:
            self.violations.append((node.lineno, f"from {node.module} import ..."))
        if node.module == "os":
            for alias in node.names:
                if alias.name in _OS_FORBIDDEN_NAMES:
                    self.violations.append(
                        (node.lineno, f"from os import {alias.name}")
                    )
                    self._os_imported_local_names.add(alias.asname or alias.name)
        self.generic_visit(node)


def find_tree_survey_references(module_path: Path) -> list[tuple[int, str]]:
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))
    visitor = TreeSurveyVisitor()
    visitor.visit(tree)
    return visitor.violations


def test_pathspec_module_exists():
    assert _TARGET_MODULE.is_file(), f"expected {_TARGET_MODULE} to exist"


def test_emit_module_exists():
    assert _EMIT_MODULE.is_file(), f"expected {_EMIT_MODULE} to exist"


def test_pathspec_has_no_tree_survey_or_subprocess_reference():
    violations = find_tree_survey_references(_TARGET_MODULE)
    assert violations == [], (
        "coordinator_core/ops/dispatch_emit/pathspec.py references a "
        f"tree-survey or subprocess surface (forbidden by AC3/AC9): {violations}"
    )


def test_emit_has_no_tree_survey_or_subprocess_reference():
    violations = find_tree_survey_references(_EMIT_MODULE)
    assert violations == [], (
        "coordinator_core/ops/dispatch_emit/emit.py references a "
        f"tree-survey or subprocess surface (forbidden by AC3/AC9): {violations}"
    )


@pytest.mark.parametrize("module_path", _GUARDED_MODULES, ids=lambda p: p.name)
def test_guarded_modules_have_no_tree_survey_or_subprocess_reference(module_path):
    violations = find_tree_survey_references(module_path)
    assert violations == [], (
        f"{module_path.relative_to(_REPO_ROOT)} references a tree-survey or "
        f"subprocess surface (forbidden by AC3/AC9): {violations}"
    )


def test_gate_detects_a_planted_os_walk_reference(tmp_path):
    fixture = tmp_path / "fixture_planted_walk.py"
    fixture.write_text(
        "import os\n\n"
        "def scan():\n"
        "    return list(os.walk('.'))\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("walk" in reason for _, reason in violations)


def test_gate_detects_a_planted_path_glob_reference(tmp_path):
    fixture = tmp_path / "fixture_planted_glob.py"
    fixture.write_text(
        "from pathlib import Path\n\n"
        "def scan():\n"
        "    return list(Path('.').rglob('*.py'))\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("rglob" in reason for _, reason in violations)


def test_gate_detects_a_planted_subprocess_call(tmp_path):
    fixture = tmp_path / "fixture_planted_subprocess.py"
    fixture.write_text(
        "import subprocess\n\n"
        "def scan():\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("run" in reason for _, reason in violations)


def test_gate_detects_a_planted_from_os_import_walk_bare_call(tmp_path):
    fixture = tmp_path / "fixture_planted_from_os_import_walk.py"
    fixture.write_text(
        "from os import walk\n\n"
        "def scan():\n"
        "    return list(walk('.'))\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("from os import walk" in reason for _, reason in violations)
    assert any("walk(...)" in reason for _, reason in violations)


def test_gate_detects_a_planted_from_os_import_listdir_bare_call(tmp_path):
    fixture = tmp_path / "fixture_planted_from_os_import_listdir.py"
    fixture.write_text(
        "from os import listdir\n\n"
        "def scan():\n"
        "    return listdir('.')\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("listdir" in reason for _, reason in violations)


def test_gate_detects_a_planted_from_os_import_scandir_bare_call(tmp_path):
    fixture = tmp_path / "fixture_planted_from_os_import_scandir.py"
    fixture.write_text(
        "from os import scandir\n\n"
        "def scan():\n"
        "    return list(scandir('.'))\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("scandir" in reason for _, reason in violations)


def test_gate_detects_a_planted_from_os_import_system_bare_call(tmp_path):
    fixture = tmp_path / "fixture_planted_from_os_import_system.py"
    fixture.write_text(
        "from os import system\n\n"
        "def scan():\n"
        "    return system('git status')\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("system" in reason for _, reason in violations)


def test_gate_detects_a_planted_from_os_import_popen_bare_call(tmp_path):
    fixture = tmp_path / "fixture_planted_from_os_import_popen.py"
    fixture.write_text(
        "from os import popen\n\n"
        "def scan():\n"
        "    return popen('git status').read()\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("popen" in reason for _, reason in violations)


def test_gate_detects_a_planted_getattr_os_walk_indirection(tmp_path):
    fixture = tmp_path / "fixture_planted_getattr_walk.py"
    fixture.write_text(
        "import os\n\n"
        "def scan():\n"
        "    return list(getattr(os, 'walk')('.'))\n",
        encoding="utf-8",
    )

    violations = find_tree_survey_references(fixture)

    assert violations
    assert any("getattr(os" in reason for _, reason in violations)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
