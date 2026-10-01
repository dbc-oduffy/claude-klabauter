"""Guard: no production module invokes a get_op_handler result outside the entry seam."""
from __future__ import annotations

import ast
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2]
_REPO = _CORE.parent

ALLOWLIST = {
    "coordinator_core/ipc.py": "dispatch core",
    "coordinator_core/warm/entry_seam.py": "the seam",
    "coordinator_core/ops/session/guard_roster_ops.py": "metadata read, never invoked",
}


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _files_calling_get_op_handler(root: Path) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*.py"):
        if "tests" in path.relative_to(root).parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "get_op_handler(" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        if any(isinstance(n, ast.Call) and _call_name(n) == "get_op_handler" for n in ast.walk(tree)):
            found.add(path.relative_to(root.parent).as_posix())
    return found


def test_only_allowlisted_modules_call_get_op_handler():
    found = _files_calling_get_op_handler(_CORE)
    offenders = sorted(found - set(ALLOWLIST))
    assert not offenders, (
        f"{offenders[0]} calls get_op_handler; use reentrant_dispatch or reentrant_dispatch_async."
    )
    stale = sorted(set(ALLOWLIST) - found)
    assert not stale, f"{stale[0]} no longer calls get_op_handler; drop it from ALLOWLIST."


def test_collector_reports_an_offending_module(tmp_path):
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "from coordinator_core.ipc import get_op_handler\n"
        "handler = get_op_handler('x'); handler({})\n",
        encoding="utf-8",
    )
    (pkg / "clean.py").write_text("x = 1\n", encoding="utf-8")
    assert _files_calling_get_op_handler(pkg) == {"coordinator_core/bad.py"}
