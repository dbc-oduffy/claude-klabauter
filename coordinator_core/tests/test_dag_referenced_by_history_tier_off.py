"""Call-shape guard: dag.referenced_by* must never reach the git-history tier.

Tier 3 of resolve_target spawns `git log --all` per (node, edge) pair and can
only return the 'git-history' sentinel, which these scans collapse onto the
unresolved branch. The scans therefore resolve through `resolve_target_on_disk`
(tiers 1-2 only, never spawns) and must not call `resolve_target` at all.
"""
import ast
from pathlib import Path

import coordinator_core.dag as dag

_SCANS = ("referenced_by", "referenced_by_indexed")


def _calls_to(fn: ast.FunctionDef, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]


def test_referenced_by_scans_pass_include_history_tier_false():
    tree = ast.parse(Path(dag.__file__).read_text(encoding="utf-8"))
    fns = {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in _SCANS
    }
    assert set(fns) == set(_SCANS)
    for name, fn in fns.items():
        assert not _calls_to(fn, "resolve_target"), (
            f"{name}: must resolve via resolve_target_on_disk, never resolve_target "
            "(its tier 3 spawns `git log --all` per edge)"
        )
    assert _calls_to(fns["referenced_by"], "resolve_target_on_disk"), (
        "referenced_by no longer resolves refs through resolve_target_on_disk"
    )
