"""`no_console_creationflags()` returns a kwargs mapping, never a flag value.

Passed as `creationflags=`, the POSIX `{}` is not 0, so subprocess raises
"creationflags is only supported on Windows platforms"; on Windows the dict is
a TypeError. It must be splatted (`**_NO_CONSOLE`).
"""
import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = (_REPO / "coordinator_core", _REPO / "coordinator" / "bin")
_HELPERS = {"no_console_creationflags", "no_console_kwargs"}


def _helper_name(call: object) -> str:
    if not isinstance(call, ast.Call):
        return ""
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def _misuses(path: Path) -> list:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return []
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _helper_name(node.value) in _HELPERS:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound |= {t.id for t in targets if isinstance(t, ast.Name)}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "creationflags":
            v = node.value
            if (isinstance(v, ast.Name) and v.id in bound) or _helper_name(v) in _HELPERS:
                hits.append(f"{path.relative_to(_REPO)}:{v.lineno}")
    return hits


def test_no_console_mapping_is_never_passed_as_creationflags():
    hits = [h for root in _ROOTS for p in root.rglob("*.py") for h in _misuses(p)]
    assert hits == [], "splat it as **<mapping>, never creationflags=<mapping>: " + ", ".join(hits)
