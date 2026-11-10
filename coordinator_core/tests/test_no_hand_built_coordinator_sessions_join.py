"""Standing gate: no module may hand-build a ``coordinator-sessions`` hub path.

``session_hub_path`` owns the spelling (``hub_for_common`` / ``hub_for_root`` /
``session_dir_for``). A reader that joins ``".git"`` + ``"coordinator-sessions"`` itself looks
in the legacy location and silently misses the hub a sandboxed child writes.

AST, not grep: only a string materialized as a Python constant can reach a path join, so this
walks ``ast.Constant`` / ``ast.JoinedStr`` and skips docstrings. Flagged shapes: the exact
segment used in a ``/`` join, a path-building call (``os.path.join``, ``Path``, ``joinpath``,
``glob``), bound to a module constant, embedded in an f-string, or inside a separator-bearing
literal such as ``"/coordinator-sessions/"``.

Scope is non-test code: test fixtures seed the legacy layout on purpose in places.
Allowlist entries are repo-relative paths, named, never a rationale-fit match.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = ("coordinator_core/", "coordinator/", "scripts/")
_NAME = "coordinator-sessions"

_ALLOW = {
    "coordinator_core/session_hub_path.py",
    "coordinator_core/tests/test_no_hand_built_coordinator_sessions_join.py",
    # Stdlib-only scripts that cannot import the engine: each applies session_hub_path's
    # two-line rule (".git" named -> <parent>/.coordinator-local/coordinator-sessions, else
    # <common>/coordinator-sessions) inline.
    "coordinator/bin/commit-scope-report.py",
    "coordinator/bin/coordinator-prepare-commit-msg.py",
    "coordinator/bin/coordinator-session-loe.py",
    "coordinator/bin/handoff-loe-summary.py",
    "coordinator/bin/wsc-session-disposition.py",
    "coordinator/lib/receiver_state_reader.py",
}

_JOIN_CALLS = {
    "join", "joinpath", "Path", "PurePath", "PurePosixPath", "PureWindowsPath",
    "glob", "rglob", "with_name", "relative_to",
}


def _tracked_py() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", *[r + "*.py" for r in _SCAN_ROOTS]],
        check=True, capture_output=True, **no_console_creationflags(),
    ).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p]


def _is_test_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return "/tests/" in rel or name.startswith("test_") or name == "conftest.py"


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _docstring_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _has_sep_around(value: str) -> bool:
    i = value.find(_NAME)
    while i != -1:
        before = value[i - 1] if i else ""
        after = value[i + len(_NAME)] if i + len(_NAME) < len(value) else ""
        if before in ("/", "\\") or after in ("/", "\\"):
            return True
        i = value.find(_NAME, i + 1)
    return False


def violations(source: str) -> list[int]:
    """Line numbers in *source* where a ``coordinator-sessions`` join is hand-built."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    docs = _docstring_ids(tree)
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    hits: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if _NAME not in node.value or id(node) in docs:
            continue
        parent = parents.get(id(node))
        if isinstance(parent, ast.JoinedStr):
            hits.append(node.lineno)
        elif _has_sep_around(node.value):
            hits.append(node.lineno)
        elif node.value == _NAME:
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div):
                hits.append(node.lineno)
            elif isinstance(parent, ast.Call) and _call_name(parent) in _JOIN_CALLS:
                hits.append(node.lineno)
            elif isinstance(parent, (ast.Assign, ast.AnnAssign)) and isinstance(
                parents.get(id(parent)), ast.Module
            ):
                hits.append(node.lineno)
    return sorted(set(hits))


def test_no_hand_built_coordinator_sessions_join_outside_session_hub_path():
    found: dict[str, list[int]] = {}
    for rel in _tracked_py():
        if rel in _ALLOW or _is_test_path(rel):
            continue
        try:
            text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _NAME not in text:
            continue
        lines = violations(text)
        if lines:
            found[rel] = lines
    assert not found, (
        "hand-built 'coordinator-sessions' join(s); resolve through "
        f"coordinator_core.session_hub_path: {found}"
    )


@pytest.mark.parametrize(
    "src",
    [
        'import os\nos.path.join(root, ".git", "coordinator-sessions", sid)\n',
        'from pathlib import Path\np = Path(root) / ".git" / "coordinator-sessions"\n',
        'HUB = "coordinator-sessions"\n',
        'p = f"{root}/.git/coordinator-sessions/{sid}"\n',
        'p = root + "/coordinator-sessions/" + sid\n',
    ],
)
def test_scanner_flags_each_join_shape(src):
    assert violations(src)


@pytest.mark.parametrize(
    "src",
    [
        '"""Names coordinator-sessions in prose."""\nx = 1\n',
        'msg = "no coordinator-sessions dir yet"\n',
        'from coordinator_core.session_hub_path import hub_for_root\nhub_for_root(cwd)\n',
    ],
)
def test_scanner_passes_prose_and_resolver_use(src):
    assert violations(src) == []
