"""The housekeeping directive is wired into both complete ceremonies, and the door serves the live op.

Caller-side sibling of `coordinator_core/tests/test_housekeeping_is_the_one_job.py`, which asserts
which key is dispatchable; this module asserts who calls it. Asserts by AST over source, because
registry membership depends on import order.

Negative-spec:
  - Does not assert the start-side call site. That lives in coordinator-content-repo's
    `coordinator/commands/workday-start.md` step 1.47, which this repo does not own and must not
    read at test time.
  - Asserts nothing about timing: `coordinator_core/housekeeping/tests/test_brightline.py` owns it.
  - Does not re-test dispatchability: `test_housekeeping_is_the_one_job.py` owns it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI = "handoff-housekeeping"
_BRIEFS = (
    "coordinator_core/workday_complete/brief.py",
    "coordinator_core/workweek_complete/brief.py",
)
_DOOR = "coordinator/bin/handoff-housekeeping.py"


def _manifest_members(tree: ast.Module) -> List[str]:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "CONSUMES_MANIFEST" for t in targets):
            if isinstance(value, (ast.Tuple, ast.List)):
                return [e.value for e in value.elts if isinstance(e, ast.Constant)]
    return []


def _is_housekeeping_directive(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_directive"
        and any(
            kw.arg == "cli" and isinstance(kw.value, ast.Constant) and kw.value.value == _CLI
            for kw in node.keywords
        )
    )


def _brief_failure(source: str) -> str | None:
    """Return a failure reason, or None when the directive is present, unconditional and declared."""
    tree = ast.parse(source)
    if _CLI not in _manifest_members(tree):
        return "not a member of CONSUMES_MANIFEST"
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_directives"),
        None,
    )
    if fn is None:
        return "no _build_directives"
    parent = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
    calls = [n for n in ast.walk(fn) if _is_housekeeping_directive(n)]
    if not calls:
        return "no housekeeping _directive call"
    for call in calls:
        node = call
        while node is not fn:
            node = parent[node]
            if isinstance(node, (ast.If, ast.IfExp)):
                break
        else:
            return None
    return "housekeeping _directive is conditional"


@pytest.mark.parametrize("rel", _BRIEFS)
def test_complete_brief_carries_unconditional_housekeeping_directive(rel: str) -> None:
    source = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    assert _brief_failure(source) is None, f"{rel}: {_brief_failure(source)}"


def test_door_imports_the_live_handler() -> None:
    tree = ast.parse((_REPO_ROOT / _DOOR).read_text(encoding="utf-8"))
    found = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "coordinator_core.housekeeping.cycle"
        and any(a.name == "_handler" for a in n.names)
        for n in ast.walk(tree)
    )
    assert found, f"{_DOOR} must import _handler from coordinator_core.housekeeping.cycle"


_SYNTH = '''
CONSUMES_MANIFEST: tuple[str, ...] = ("other", "handoff-housekeeping")

def _build_directives():
    return [
        _directive("a", cli="other", args=[]),
{hk}
    ]
'''


def test_negative_control_checker_can_go_red() -> None:
    hk = '        _directive("h", cli="handoff-housekeeping", args=[]),'
    assert _brief_failure(_SYNTH.format(hk=hk)) is None
    assert _brief_failure(_SYNTH.format(hk="")) is not None
    cond = '        *([_directive("h", cli="handoff-housekeeping", args=[])] if x else []),'
    assert _brief_failure(_SYNTH.format(hk=cond)) is not None
