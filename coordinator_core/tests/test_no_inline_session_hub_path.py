"""Source-scan ratchet: no non-test coordinator_core file builds the session hub path itself.

The hub location comes from ``coordinator_core.session_hub_path.hub_for_common``. A string
literal exactly equal to the hub name, or a path join against ``HUB_NAME``, outside the
resolver and the legacy migration is a straggler that resolves the pre-migration
``<git dir>/coordinator-sessions`` location. Pure AST over the tree, zero spawns.
Blind spot: an aliased ``HUB_NAME`` import or a longer literal containing the name is not caught.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List

_CORE = Path(__file__).resolve().parents[1]
_HUB_LITERAL = "coordinator-sessions"

# The resolver itself, and the one reader of the legacy location (migrate_hub.py).
_ALLOWED = {"session_hub_path.py", "migrate_hub.py"}


def _is_test_path(rel: Path) -> bool:
    return (
        "tests" in rel.parts
        or rel.name.startswith("test_")
        or rel.name == "conftest.py"
    )


def _is_hub_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "HUB_NAME"


def find_hub_builders(source: str) -> List[int]:
    """Line numbers where *source* builds the hub path from the literal or ``HUB_NAME``."""
    hits: List[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and node.value == _HUB_LITERAL:
            hits.append(node.lineno)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _is_hub_name(node.left) or _is_hub_name(node.right):
                hits.append(node.lineno)
        elif isinstance(node, ast.Call) and any(_is_hub_name(a) for a in node.args):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("join", "joinpath", "Path"):
                hits.append(node.lineno)
    return hits


def _scan_tree() -> List[str]:
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(_CORE):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = Path(dirpath) / fn
            rel = path.relative_to(_CORE)
            if fn in _ALLOWED or _is_test_path(rel):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if _HUB_LITERAL not in text and "HUB_NAME" not in text:
                continue
            for line in find_hub_builders(text):
                found.append(f"coordinator_core/{rel.as_posix()}:{line}")
    return found


def test_no_non_test_file_builds_the_hub_path_inline() -> None:
    assert _scan_tree() == [], (
        "route through coordinator_core.session_hub_path.hub_for_common"
    )


def test_scan_flags_a_literal_join() -> None:
    assert find_hub_builders('p = git_dir / "coordinator-sessions"\n') == [1]


def test_scan_flags_a_hub_name_join() -> None:
    assert find_hub_builders("p = common / HUB_NAME\n") == [1]
    assert find_hub_builders("p = os.path.join(common, HUB_NAME)\n") == [1]


def test_scan_accepts_resolver_use_and_name_comparison() -> None:
    src = "hub = hub_for_common(common)\nok = parents[1].name != HUB_NAME\n"
    assert find_hub_builders(src) == []


def test_scan_ignores_prose_mentioning_the_hub() -> None:
    assert find_hub_builders('"""see .coordinator-local/coordinator-sessions/ here"""\n') == []
