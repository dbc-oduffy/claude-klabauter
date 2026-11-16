"""Bridge, peer and router mains are engine-class: none calls tenant_priority.apply_to_self()."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

ROOT = Path(__file__).resolve().parents[3]
ENTRIES = (
    ROOT / "coordinator_core" / "agent_pipe" / "__main__.py",
    ROOT / "coordinator_core" / "agent_pipe" / "peer.py",
    ROOT / "coordinator" / "bin" / "unimatrix-peer-router.py",
)


def _apply_calls(tree: ast.AST) -> list[int]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "apply_to_self":
                out.append(node.lineno)
    return out


def _main_of(path: Path) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    assert mains, f"{path.name} has no module-level main"
    return mains[0]


@pytest.mark.parametrize("path", ENTRIES, ids=lambda p: p.name)
def test_entry_main_does_not_lower_itself(path: Path) -> None:
    assert not _apply_calls(_main_of(path))


@pytest.mark.parametrize("path", ENTRIES, ids=lambda p: p.name)
def test_entry_module_never_calls_apply_to_self(path: Path) -> None:
    assert not _apply_calls(ast.parse(path.read_text(encoding="utf-8")))


def test_scan_detects_a_call() -> None:
    src = "def main():\n    tenant_priority.apply_to_self()\n"
    assert _apply_calls(ast.parse(src)) == [2]
