"""plan.overlap_edges: write-overlap ordering edges across certified plans, derived, never written.

Contract (COMPUTE_ONLY, scope `common_dir`). Wire params: `plans` (list[str], required, 1 to
MAX_PLANS entries), repo-relative `docs/plans/*.md`, resolved under `main_worktree_root(repo_root)`.
A longer list is refused before any path resolves. Duplicates are dropped and input order does not
change the reply. Reply:

    {"set": [rel, ...],                     # certified members, sorted
     "edges": [{"before": rel, "after": rel, "paths": [norm path, ...],
                "rows": [{"after_row": id, "before_row": id, "path": norm path}]}],
     "cycles": [{"plans": [later, earlier], "path": norm path, "detail": str}],
     "uncertified": [rel, ...],             # no non-empty mise_prepped_sha; excluded
     "unreadable": [{"plan": rel, "detail": str}]}

Direction is `plan_seam_fix.derive_edges`': the lexicographically later path follows the earlier
one. Edges come from declared writes of rows whose disposition is not closed
(`plan_writes_lean`), so the graph orders every pair `read_spine`-live rows order, and more. There
is no run-order list. The op opens no file for write, spawns no process, accepts no
caller-supplied root, persists nothing, and does not recompute `mise_prepped_sha`.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.plan_seam_check import _contained_rel, _declared_append_hubs
from coordinator_core.ops.plan_seam_fix import derive_edges
from coordinator_core.ops.plan_writes_lean import LeanPlan, read_lean_plan

GENERATES: list = []

# Example-Game-Repo's Phase 0 set is 183 certified plans; 200 is the bound that premise is held to.
MAX_PLANS = 200


def _parse_params(params: dict, root: Path) -> List[str]:
    """Distinct `docs/plans/*.md` rels, sorted. The bound is checked before any path resolves."""
    plans_raw = params.get("plans")
    if not isinstance(plans_raw, list) or not plans_raw or not all(
        isinstance(p, str) and p.strip() for p in plans_raw
    ):
        raise ValueError("plans must be a non-empty list of repo-relative plan paths")
    if len(plans_raw) > MAX_PLANS:
        raise ValueError(f"plans: {len(plans_raw)} entries, bound {MAX_PLANS}; pass a smaller set")
    rels = set()
    for raw in plans_raw:
        rel = _contained_rel(raw, root)
        if rel is None:
            raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
        if not rel.endswith(".md") or not (root / rel).is_file():
            raise ValueError(f"no such plan: {raw!r}")
        if posixpath.dirname(rel) != "docs/plans":
            raise ValueError(f"plan.overlap_edges reads docs/plans/ plans only: {raw!r}")
        rels.add(rel)
    return sorted(rels)


def _group_edges(edges_added: List[dict]) -> List[dict]:
    grouped: Dict[tuple, dict] = {}
    for e in edges_added:
        key = (e["depends_on"]["plan"], e["plan"])
        entry = grouped.setdefault(key, {"before": key[0], "after": key[1], "paths": set(), "rows": set()})
        entry["paths"].add(e["path"])
        entry["rows"].add((e["chunk"], e["depends_on"]["chunk"], e["path"]))
    return [
        {
            "before": g["before"],
            "after": g["after"],
            "paths": sorted(g["paths"]),
            "rows": [{"after_row": a, "before_row": b, "path": p} for a, b, p in sorted(g["rows"])],
        }
        for _, g in sorted(grouped.items())
    ]


def _overlap_edges(params: dict, root: Path,
                   read: Callable[[str, Path], LeanPlan] = read_lean_plan) -> dict:
    """The reply of the module docstring; a function of the plan bytes alone."""
    rels = _parse_params(params, root)
    certified: Dict[str, LeanPlan] = {}
    uncertified: List[str] = []
    unreadable: List[dict] = []
    for rel in rels:
        plan = read(rel, root)
        plan.load_rows()
        if plan.unreadable:
            unreadable.append({"plan": rel, "detail": plan.unreadable})
        elif not plan.certified_sha:
            uncertified.append(rel)
        else:
            certified[rel] = plan
    derived = derive_edges(certified, _declared_append_hubs(root), root)
    cycles = sorted(
        ({"plans": list(c["plans"]), "path": c["path"], "detail": c["detail"]}
         for c in derived.skipped_cycles),
        key=lambda c: (c["path"], c["plans"]),
    )
    return {
        "set": sorted(certified),
        "edges": _group_edges(derived.edges_added),
        "cycles": cycles,
        "uncertified": uncertified,
        "unreadable": unreadable,
    }


@register_op("plan.overlap_edges")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.overlap_edges" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.overlap_edges requires a resolved repo_root")
    return _overlap_edges(params, Path(main_worktree_root(repo_root)))
