"""One single-process AST check that no production module-level import crosses a declared forbidden layer edge."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from coordinator_core.pyimports import (
    ImportRecord,
    import_test_is_type_checking,
    module_name_for_file,
    resolve_relative_module,
)
from coordinator_core.tests.import_layers_decl import FORBIDDEN_EDGES, LAYERS, layer_of

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = frozenset({"tests", "test", "__pycache__"})

_SOURCE_LAYERS = frozenset(e.source_layer for e in FORBIDDEN_EDGES)
_EDGES = {(e.source_layer, e.target_layer): e for e in FORBIDDEN_EDGES}
_TARGET_PREFIXES = {p for e in FORBIDDEN_EDGES for p in LAYERS[e.target_layer]}
# trap: a file can only cross an edge if an import line names a target prefix's last segment, or a
# parenthesised/continued `from <parent package> import (` hides the names on later lines.
_PREFILTER = re.compile(
    r"^[ \t]*(?:from|import)\b[^\n]*(?:\b(?:%s)\b|(?<=from )(?:\.+|%s) +import *[(\\])"
    % (
        "|".join(sorted({p.rsplit(".", 1)[-1] for p in _TARGET_PREFIXES})),
        "|".join(sorted({re.escape(p.rsplit(".", 1)[0]) for p in _TARGET_PREFIXES if "." in p})),
    ),
    re.M,
)


class _ModuleLevelImports(ast.NodeVisitor):
    """Import records that execute at import time: no function bodies, no TYPE_CHECKING branch."""

    def __init__(self) -> None:
        self.records: "list[ImportRecord]" = []

    def visit_If(self, node: "ast.If") -> None:
        if import_test_is_type_checking(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_FunctionDef(self, node: "ast.FunctionDef") -> None:
        return

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]
    visit_Lambda = visit_FunctionDef  # type: ignore[assignment]

    def visit_Import(self, node: "ast.Import") -> None:
        self.records.extend(ImportRecord(a.name, (), 0, node.lineno) for a in node.names)

    def visit_ImportFrom(self, node: "ast.ImportFrom") -> None:
        names = tuple(a.name for a in node.names)
        self.records.append(ImportRecord(node.module or "", names, node.level or 0, node.lineno))


def _source_files(repo_root: Path) -> "list[Path]":
    roots = {
        repo_root.joinpath(*prefix.split("."))
        for layer in _SOURCE_LAYERS
        for prefix in LAYERS[layer]
    }
    files: "set[Path]" = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            files.update(
                Path(dirpath, f)
                for f in filenames
                if f.endswith(".py") and not f.startswith("test_")
            )
    return sorted(files)


def _targets(record: ImportRecord, module: str, is_package: bool) -> "list[str]":
    base = record.module
    if record.level:
        resolved = resolve_relative_module(base, record.level, module, is_package)
        if resolved is None:
            return []
        base = resolved
    # `from pkg import sub` reaches `pkg.sub` when `sub` is a submodule.
    return [base, *(f"{base}.{n}" for n in record.names if n != "*")]


def find_violations(repo_root: Path) -> "list[str]":
    """Return one `path:lineno: import — root cause. Allowed route: …` line per forbidden module-level import."""
    violations: "list[str]" = []
    for path in _source_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        module = module_name_for_file(rel)
        source_layer = layer_of(module) if module else None
        if source_layer not in _SOURCE_LAYERS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _PREFILTER.search(text):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        visitor = _ModuleLevelImports()
        visitor.visit(tree)
        for record in visitor.records:
            for target in _targets(record, module, path.name == "__init__.py"):
                edge = _EDGES.get((source_layer, layer_of(target)))
                if edge is not None:
                    violations.append(
                        f"{rel}:{record.lineno}: imports {target} — {edge.root_cause}. "
                        f"Allowed route: {edge.allowed_route}"
                    )
                    break
    return violations


def _write(repo_root: Path, rel: str, source: str) -> None:
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


_BASH_GUARD = "coordinator_core/bash_guards/injected_guard.py"


def test_tree_has_no_forbidden_layer_import() -> None:
    assert find_violations(_REPO_ROOT) == []


def test_injected_edge_names_root_cause_and_allowed_route(tmp_path: Path) -> None:
    _write(tmp_path, _BASH_GUARD, "from coordinator_core.hooks import envelope\n")
    edge = _EDGES[("bash_guards", "hooks")]
    (message,) = find_violations(tmp_path)
    assert message.startswith(f"{_BASH_GUARD}:1:")
    assert edge.root_cause in message
    assert f"Allowed route: {edge.allowed_route}" in message


def test_from_package_import_submodule_shape_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, _BASH_GUARD, "from coordinator_core import hooks\n")
    (message,) = find_violations(tmp_path)
    assert "imports coordinator_core.hooks" in message


def test_parenthesised_from_import_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, _BASH_GUARD, "from coordinator_core import (\n    hooks,\n)\n")
    assert len(find_violations(tmp_path)) == 1


def test_relative_import_of_forbidden_layer_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, _BASH_GUARD, "from .. import hooks\n")
    assert len(find_violations(tmp_path)) == 1


def test_type_checking_import_is_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _BASH_GUARD,
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import coordinator_core.hooks\n",
    )
    assert find_violations(tmp_path) == []


def test_function_local_import_is_not_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _BASH_GUARD,
        "def f():\n    import coordinator_core.hooks\n\n"
        "g = lambda: __import__('x')\n"
        "class C:\n    import coordinator_core.hooks\n",
    )
    (message,) = find_violations(tmp_path)
    assert message.startswith(f"{_BASH_GUARD}:6:")


def test_core_to_bin_edge_is_caught_and_tests_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "coordinator_core/ops/launcher.py", "from coordinator.bin.lib import win_argv\n")
    _write(tmp_path, "coordinator_core/ops/tests/test_launcher.py", "import coordinator.bin\n")
    _write(tmp_path, "coordinator_core/ops/test_other.py", "import coordinator.bin\n")
    (message,) = find_violations(tmp_path)
    assert message.startswith("coordinator_core/ops/launcher.py:1:")
