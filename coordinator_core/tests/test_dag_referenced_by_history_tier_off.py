"""Call-shape guard: dag.referenced_by* must never re-enable the git-history tier.

Tier 3 of resolve_target spawns `git log --all` per (node, edge) pair and can
only return the 'git-history' sentinel, which these scans collapse onto the
unresolved branch.
"""
import ast
from pathlib import Path

import coordinator_core.dag as dag

_SCANS = ("referenced_by", "referenced_by_indexed")


def _resolve_target_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "resolve_target"
    ]


def test_referenced_by_scans_pass_include_history_tier_false():
    tree = ast.parse(Path(dag.__file__).read_text(encoding="utf-8"))
    fns = {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in _SCANS
    }
    assert set(fns) == set(_SCANS)
    total = 0
    for name, fn in fns.items():
        for call in _resolve_target_calls(fn):
            total += 1
            kw = {k.arg: k.value for k in call.keywords}
            val = kw.get("include_history_tier")
            assert isinstance(val, ast.Constant) and val.value is False, (
                f"{name}: resolve_target must pass include_history_tier=False"
            )
    assert total >= 1
