"""Leaf reader for the sprint spine record (`state/roadmap/<run-id>/SPINE.md`).

Extracted (2026-08-23, AC6) so that BOTH `coordinator_core.roadmap.audit` and
`coordinator_core.ops.roadmap_dag` can reach the spine without importing each
other. `audit.py` imports `coordinator_core.ops.ceremony.records_query`, so an
`ops/` module importing `audit` would pull `coordinator_core.ops.__init__`'s
eager registration loop back through itself — the exact partially-initialized
-module cycle `execute_plan_assemble/row_spans.py`'s docstring records three
prior instances of. This module is a LEAF: `pathlib`, `typing`, and
`coordinator_core.frontmatter.schema_validate` only, nothing from
`coordinator_core.ops`.

Contract: `coordinator_core/frontmatter/schemas/spine.schema.json` (1.0.0,
vendored from DoE). Two rules that schema declares but JSON Schema cannot
express are enforced here instead, as the schema's own `cross_sprint_edges`
NEGATIVE-SPEC requires ("both are the emitter's fail-loud assertions"):
a `to` must differ from its `from`, and the edge set must be acyclic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.frontmatter.schema_validate import parse_frontmatter


class CrossSprintEdgeError(ValueError):
    pass


def read_spine(spine_path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = spine_path.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter")
    if not isinstance(fm, dict) or fm.get("kind") != "roadmap-spine":
        return None
    return fm


def find_spine(worktree_root: Path, roadmap_id: str) -> Optional[Dict[str, Any]]:
    spine_dir = worktree_root / "state" / "roadmap"
    if not spine_dir.is_dir():
        return None
    for candidate in sorted(spine_dir.glob("*/SPINE.md")):
        spine = read_spine(candidate)
        if spine is not None and spine.get("roadmap_id") == roadmap_id:
            return spine
    return None


def _assert_acyclic(pairs: List[Tuple[str, str]]) -> None:
    adjacency: Dict[str, List[str]] = {}
    for src, dst in pairs:
        adjacency.setdefault(src, []).append(dst)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: Dict[str, int] = {}

    def visit(node: str, trail: List[str]) -> None:
        colour[node] = GREY
        for nxt in adjacency.get(node, []):
            state = colour.get(nxt, WHITE)
            if state == GREY:
                cycle = trail[trail.index(nxt):] if nxt in trail else [nxt]
                raise CrossSprintEdgeError(
                    "cross_sprint_edges forms a cycle: "
                    + " -> ".join([*cycle, nxt])
                )
            if state == WHITE:
                visit(nxt, [*trail, nxt])
        colour[node] = BLACK

    for node in list(adjacency):
        if colour.get(node, WHITE) == WHITE:
            visit(node, [node])


def cross_sprint_gates(spine: Dict[str, Any]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for entry in spine.get("cross_sprint_edges") or []:
        if not isinstance(entry, dict):
            continue
        src, dst = entry.get("from"), entry.get("to")
        if not (isinstance(src, str) and src and isinstance(dst, str) and dst):
            continue
        if src == dst:
            raise CrossSprintEdgeError(
                f"cross_sprint_edges entry gates a sprint on itself: from == to == {src!r}"
            )
        pairs.append((src, dst))
    _assert_acyclic(pairs)
    return pairs
