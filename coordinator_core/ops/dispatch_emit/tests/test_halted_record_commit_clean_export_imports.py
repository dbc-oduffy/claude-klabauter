"""halted_record_commit.py imports from land_run_halt only names land_run_halt defines at HEAD.

A name defined only in an uncommitted land_run_halt.py hunk imports fine in the live tree and fails
`ImportError` in a clean export of the landed candidate.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit

pytestmark = pytest.mark.spawns_process

_REPO = Path(__file__).resolve().parents[4]
_MODULE = "coordinator_core.ops.dispatch_emit.land_run_halt"


def _head_source(rel: str) -> str | None:
    """`rel`'s text at HEAD; None when untracked there."""
    proc = subprocess.run(
        ["git", "-C", str(_REPO), "--no-optional-locks", "show", f"HEAD:{rel}"],
        capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def _defined(source: str) -> set:
    names: set = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def imported_from(source: str, module: str) -> set:
    """Every name `source` imports from `module`, nested imports included."""
    return {
        a.name for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.ImportFrom) and n.module == module for a in n.names
    }


def absent_at_head(source: str, module: str) -> list:
    """Names `source` imports from `module` that `module` does not define at HEAD."""
    head = _head_source(module.replace(".", "/") + ".py")
    assert head is not None, f"{module} is not tracked at HEAD"
    return sorted(imported_from(source, module) - _defined(head))


def test_halted_record_commit_imports_only_names_land_run_halt_defines_at_head():
    source = Path(halted_record_commit.__file__).read_text(encoding="utf-8")
    assert imported_from(source, _MODULE), "the import block moved: re-locate the anchor"
    assert absent_at_head(source, _MODULE) == []


def test_own_withhold_reader_imports_resolve_at_head():
    source = Path(halted_record_commit.__file__).read_text(encoding="utf-8")
    for module in (
        "coordinator_core.ops.dispatch_emit.ask_contract",
        "coordinator_core.ops.dispatch_emit.commit_request",
        "coordinator_core.ops.dispatch_emit.halt_review_passed",
    ):
        assert absent_at_head(source, module) == [], module


def test_checker_refuses_a_name_land_run_halt_does_not_define_at_head():
    source = f"from {_MODULE} import marker_kind, a_name_no_head_defines\n"
    assert absent_at_head(source, _MODULE) == ["a_name_no_head_defines"]


def test_unexecuted_withholds_is_the_halt_commits_own_definition():
    assert halted_record_commit.unexecuted_withholds.__module__ == halted_record_commit.__name__
