"""AST detector for a test that rewrites a tracked file after a commit without `write_seen_by_git`.

Under `core.checkStat=minimal` a same-size rewrite inside the index's mtime tick reads clean to
`git status`. `find_sites(root)` flags every `.write_text(` / `.write_bytes(` call, or call to a
`_write*` wrapper, that follows a git-commit call in the same function AND is followed, in that
function, by a read of git's dirt (a call or string naming status/porcelain, or a call named
dirty/changed). Per-function and static: a status read in a consuming test is not seen. A false
positive takes the helper or a reasoned `# racy-stat-ok: <reason>` marker on a line of the write
call; a marker without a reason is itself flagged.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_MARKER = re.compile(r"#\s*racy-stat-ok\b(?P<rest>.*)")


@dataclass(frozen=True)
class Site:
    path: Path
    lineno: int
    function: str
    call: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} {self.function}: {self.call} ({self.reason})"


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _has_commit_literal(node: ast.Call) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value == "commit":
            return True
    return False


def _is_commit(node: ast.Call) -> bool:
    return "commit" in _callee(node) or _has_commit_literal(node)


def _is_write(node: ast.Call) -> bool:
    name = _callee(node)
    if name in ("write_text", "write_bytes"):
        return True
    return name.startswith("_write") and "commit" not in name


_DIRT_NAMES = ("status", "porcelain", "dirty", "changed")


def _is_dirt_read(node: ast.Call) -> bool:
    if any(k in _callee(node).lower() for k in _DIRT_NAMES):
        return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if "status" in sub.value or "porcelain" in sub.value:
                return True
    return False


def _own_calls(fn: ast.AST) -> list[ast.Call]:
    """Calls in `fn`'s body, excluding nested function bodies (each is scanned on its own)."""
    out: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    out.sort(key=lambda c: (c.lineno, c.col_offset))
    return out


def _marker_state(lines: list[str], call: ast.Call) -> str:
    """'' when unmarked, 'ok' for a reasoned marker, 'bare' for a marker with no reason."""
    end = call.end_lineno or call.lineno
    state = ""
    for text in lines[call.lineno - 1 : end]:
        m = _MARKER.search(text)
        if not m:
            continue
        rest = m.group("rest").strip()
        if rest.startswith(":") and rest[1:].strip():
            return "ok"
        state = "bare"
    return state


def _scan_file(path: Path) -> list[Site]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    sites: list[Site] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        commit_line = None
        calls = _own_calls(fn)
        for call in calls:
            if commit_line is None:
                if _is_commit(call):
                    commit_line = call.lineno
                continue
            if call.lineno <= commit_line or not _is_write(call):
                continue
            end = call.end_lineno or call.lineno
            if not any(c.lineno > end and _is_dirt_read(c) for c in calls):
                continue
            state = _marker_state(lines, call)
            if state == "ok":
                continue
            reason = "racy-stat-ok marker has no reason" if state == "bare" else "write after commit"
            sites.append(Site(path, call.lineno, fn.name, _callee(call), reason))
    return sites


def find_sites(root: Path) -> list[Site]:
    """Flagged sites under `root`, over `test_*.py` and `conftest.py`, sorted by path and line."""
    root = Path(root)
    files = sorted({*root.rglob("test_*.py"), *root.rglob("conftest.py")})
    out: list[Site] = []
    for f in files:
        out.extend(_scan_file(f))
    out.sort(key=lambda s: (str(s.path), s.lineno))
    return out


def site_keys(root: Path) -> set[str]:
    """`relative/path.py::function` per flagged function under `root` (the ratchet's unit)."""
    root = Path(root)
    return {f"{s.path.relative_to(root).as_posix()}::{s.function}" for s in find_sites(root)}
