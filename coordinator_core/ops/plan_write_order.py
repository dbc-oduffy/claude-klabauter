"""
coordinator_core.ops.plan_write_order — JSON-RPC "plan.write_order" (COMPUTE_ONLY).

Purpose: for the certified members of a named plan set, derive the write-overlap ordering
edges and a total run order, without touching a plan. Phase 0 uses the result in place of a
``depends_on_plan`` body edit, which would void the ``mise_prepped_sha`` stamp.

Wire params:
    plans (list[str], required) — repo-relative ``docs/plans/*.md`` paths, at least one.
        Duplicates are dropped; input order does not affect the reply.

Reply:
    {"set": [rel, ...],        certified members, sorted
     "order": [rel, ...],      total run order of ``set``: declared and derived edges respected
     "edges": [{"before", "after", "paths": [path, ...],
                "rows": [{"after_row", "before_row", "path"}]}],
     "already_ordered": [{"before", "after", "path"}],
     "cycles": [{"plans": [later, earlier], "path", "detail"}],
     "uncertified": [rel, ...],   no ``mise_prepped_sha``; excluded from the derivation
     "unreadable": [{"plan", "detail"}]}

Design:
  - ``plan_seam_fix.derive_edges`` is the one derivation; this op shares it with
    ``plan.seam_fix``, so the two cannot disagree on direction (later path follows earlier).
  - Certified means a non-empty frontmatter ``mise_prepped_sha``; the body sha is not
    recomputed (mise-certify's sha leg owns freshness).
  - ``order`` is a Kahn sort of the set under the final plan graph, ties broken by path. Plans
    left on a declared ``depends_on_plan`` cycle are appended in path order.

Negative-spec:
  - Opens no file for write and never changes a plan byte or stamp.
  - Spawns no process and accepts no caller-supplied root.
  - Persists no edge anywhere; the reply is the only output.
"""

from __future__ import annotations

import heapq
import posixpath
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.plan_seam_check import _Plan, _contained_rel, _declared_append_hubs
from coordinator_core.ops.plan_seam_fix import derive_edges

GENERATES: list = []


def _parse_params(params: dict, root: Path) -> List[str]:
    plans_raw = params.get("plans")
    if not isinstance(plans_raw, list) or not plans_raw or not all(
        isinstance(p, str) and p.strip() for p in plans_raw
    ):
        raise ValueError("plans must be a non-empty list of repo-relative plan paths")
    plans: List[str] = []
    for raw in plans_raw:
        rel = _contained_rel(raw, root)
        if rel is None:
            raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
        if not (root / rel).is_file() or not rel.endswith(".md"):
            raise ValueError(f"no such plan: {raw!r}")
        if posixpath.dirname(rel) != "docs/plans":
            raise ValueError(f"plan.write_order reads docs/plans/ plans only: {raw!r}")
        if rel not in plans:
            plans.append(rel)
    return plans


def _is_certified(plan: _Plan) -> bool:
    sha = plan.fm.get("mise_prepped_sha")
    return sha is not None and bool(str(sha).strip())


def _group_edges(edges_added: List[dict]) -> List[dict]:
    grouped: Dict[tuple, dict] = {}
    for e in edges_added:
        after, before = e["plan"], e["depends_on"]["plan"]
        entry = grouped.setdefault(
            (before, after), {"before": before, "after": after, "paths": set(), "rows": set()}
        )
        entry["paths"].add(e["path"])
        entry["rows"].add((e["chunk"], e["depends_on"]["chunk"], e["path"]))
    out = []
    for key in sorted(grouped):
        entry = grouped[key]
        out.append({
            "before": entry["before"], "after": entry["after"], "paths": sorted(entry["paths"]),
            "rows": [{"after_row": a, "before_row": b, "path": p} for a, b, p in sorted(entry["rows"])],
        })
    return out


def _run_order(members: List[str], graph: Dict[str, set]) -> List[str]:
    """Kahn sort, dependencies first, ties by path; cycle remnants appended in path order."""
    deps = {m: {d for d in graph.get(m, ()) if d in graph and d != m} for m in members}
    dependents: Dict[str, List[str]] = {m: [] for m in members}
    for m, ds in deps.items():
        for d in ds:
            dependents[d].append(m)
    ready = [m for m in members if not deps[m]]
    heapq.heapify(ready)
    queued = set(ready)
    order: List[str] = []
    while ready:
        cur = heapq.heappop(ready)
        order.append(cur)
        for nxt in dependents[cur]:
            deps[nxt].discard(cur)
            if not deps[nxt] and nxt not in queued:
                queued.add(nxt)
                heapq.heappush(ready, nxt)
    placed = set(order)
    return order + [m for m in members if m not in placed]


def _write_order(params: dict, root: Path) -> dict:
    plans = _parse_params(params, root)
    certified: Dict[str, _Plan] = {}
    uncertified: List[str] = []
    unreadable: List[dict] = []
    for rel in plans:
        try:
            plan = _Plan(rel, root)
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append({"plan": rel, "detail": str(exc)})
            continue
        if not _is_certified(plan):
            uncertified.append(rel)
            continue
        plan.load_rows()
        if plan.unreadable:
            unreadable.append({"plan": rel, "detail": plan.unreadable})
            continue
        certified[rel] = plan

    derived = derive_edges(certified, _declared_append_hubs(root), root)
    members = sorted(certified)
    return {
        "set": members,
        "order": _run_order(members, derived.graph),
        "edges": _group_edges(derived.edges_added),
        "already_ordered": [
            {"before": earlier, "after": later, "path": path}
            for later, earlier, path in sorted(
                set(derived.already_ordered), key=lambda t: (t[1], t[0], t[2])
            )
        ],
        "cycles": sorted(
            ({"plans": c["plans"], "path": c["path"], "detail": c["detail"]} for c in derived.skipped_cycles),
            key=lambda c: (c["plans"], c["path"]),
        ),
        "uncertified": sorted(uncertified),
        "unreadable": sorted(unreadable, key=lambda u: u["plan"]),
    }


@register_op("plan.write_order")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.write_order" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.write_order requires a resolved repo_root")
    return _write_order(params, Path(main_worktree_root(repo_root)))
