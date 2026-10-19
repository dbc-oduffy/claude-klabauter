"""
coordinator_core.ops.plan_seam_fix — JSON-RPC "plan.seam_fix".

Purpose: iron out in-set ``writes-collision`` seams by machine. For every pair of plans in
the given set that ``plan.seam_check`` reports as colliding, mint one ``depends_on_plan`` edge
so the pair is ordered, in a stable order, and write it into the later plan's
``plan-tasks`` spine. Idempotent: a second run finds no collision an edge can clear and
writes nothing.

Wire params:
    plans (list[str], required) — repo-relative ``docs/plans/*.md`` paths, at least one.
        Only collisions between members of this set are fixed (``named_set`` semantics).
    dry_run (bool, default false) — compute and reply; open nothing for write.

Reply:
    {"edges_added": [{"plan", "chunk", "depends_on": {"plan", "chunk"}, "path"}],
     "skipped_cycles": [{"class": "seam-fix-cycle", "plans": [later, earlier], "path", "detail"}],
     "skipped_plans": [{"plan", "detail"}],
     "files": [repo-relative paths written], "converged": bool}
    ``edges_added`` under dry_run lists the edges that WOULD be written; ``files`` is empty.
    ``skipped_plans``: a plan whose write raised ``MutateAbort`` (e.g. a row failing the
    current schema) is left byte-identical, its edges leave ``edges_added``, and the rest of
    the set is still written; ``detail`` is the abort message. Other exceptions propagate.
    Always empty under dry_run (nothing is written, so no abort can occur).
    ``converged``: a seam check over the same set (after the writes; against the planned
    graph under dry_run) has no in-set writes-collision left.

Design:
  - ``derive_edges(pset, hubs, root)`` is the one derivation of these edges (pure; ``_fix``
    adds the writes). ``plan.write_order`` shares it, so the two ops cannot disagree on order.
  - Direction: for a colliding pair the plan with the lexicographically later repo-relative
    path depends on the earlier one. Pairs are processed in (later plan, earlier plan, path)
    order; the first edge that orders a pair also orders every pair it implies transitively.
  - Edge rows: every live row of the later plan whose writes cover the colliding path, in
    ``depends_on`` topological order (spine order breaking ties). An edged row is withheld
    from the seam check's writes, so a row left bare would keep the pair colliding. Edge target: the earlier plan's
    topo-last row covering the path, so the edge holds until the last writer of that path
    has landed. ``chunk`` is required by the schema (``status`` would gate on the whole plan).
  - gate_kind ``output-consumption-runtime``: the only member of the enum that is a runtime
    withhold of the dependent row until the named row is ``coded``; ``epistemic-premise``
    means "the predecessor decides what this row writes" and would also license an
    undeclared ``writes``, which is not the relation here.
  - Cycle check: an edge later->earlier is not written when the earlier plan already reaches
    the later one through the graph so far (existing edges plus edges added this run). The
    pair is then already ordered, opposite to the stable direction, so it is reported as a
    ``seam-fix-cycle`` finding. A pair where the later plan already reaches the earlier one
    needs no edge and no finding.
  - Writes reuse ``plan_tasks_mutate``'s locked read-modify-write, row validation, and
    ``_patch_body`` (re-serializes only the touched rows; every other byte is kept).

Negative-spec:
  - Never commits and accepts no caller-supplied root.
  - Never touches a plan outside the given set, and never edits plan frontmatter.
  - A plan whose spine is absent or unreadable is not written; its collisions stay.
  - Appends-only pairs are not collisions (``plan.seam_check`` already ignores them).
"""

from __future__ import annotations

import copy
import posixpath
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import MutateAbort
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.plan_seam_check import (
    COLLISION,
    _Plan,
    _collision_findings,
    _contained_rel,
    _declared_append_hubs,
    _covers,
    _paths_and_prefixes,
)

GENERATES: list = []
MUTATES = ["docs/plans/*.md"]

GATE_KIND = "output-consumption-runtime"
CYCLE_CLASS = "seam-fix-cycle"


def _parse_params(params: dict, root: Path) -> Tuple[List[str], bool]:
    plans_raw = params.get("plans")
    if not isinstance(plans_raw, list) or not plans_raw or not all(
        isinstance(p, str) and p.strip() for p in plans_raw
    ):
        raise ValueError("plans must be a non-empty list of repo-relative plan paths")
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a bool")
    plans: List[str] = []
    for raw in plans_raw:
        rel = _contained_rel(raw, root)
        if rel is None:
            raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
        if not (root / rel).is_file() or not rel.endswith(".md"):
            raise ValueError(f"no such plan: {raw!r}")
        if posixpath.dirname(rel) != "docs/plans":
            raise ValueError(f"plan.seam_fix writes docs/plans/ plans only: {raw!r}")
        if rel not in plans:
            plans.append(rel)
    return plans, dry_run


def _topo_ids(plan: _Plan) -> List[str]:
    """Live row ids in ``depends_on`` order; spine order breaks ties and absorbs cycles."""
    ids = [r.id for r in plan.live_rows]
    known = set(ids)
    deps: Dict[str, set] = {}
    for rid in ids:
        edges = (plan.raw_by_id.get(rid) or {}).get("depends_on") or []
        deps[rid] = {
            e["chunk"] for e in edges
            if isinstance(e, dict) and isinstance(e.get("chunk"), str) and e["chunk"] in known
        }
    order: List[str] = []
    placed: set = set()
    pending = list(ids)
    while pending:
        ready = next((r for r in pending if deps[r] <= placed), pending[0])
        pending.remove(ready)
        placed.add(ready)
        order.append(ready)
    return order


def _writer_rows(plan: _Plan, order: List[str], path: str) -> List[str]:
    by_id = {r.id: r for r in plan.live_rows}
    hits = []
    for rid in order:
        row = by_id[rid]
        if row.writes is UNDECLARED and not getattr(row, "writes_under", ()):
            continue
        if _covers(*_paths_and_prefixes([row]), path):
            hits.append(rid)
    return hits


def _reaches(graph: Dict[str, set], src: str, dst: str) -> bool:
    seen: set = set()
    stack = [src]
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        if cur not in seen:
            seen.add(cur)
            stack.extend(graph.get(cur, ()))
    return False


def _collision_pairs(pset: Dict[str, _Plan], hubs: set, root: Path) -> List[Tuple[str, str, str]]:
    """Distinct (later plan, earlier plan, path), sorted, over the named-set collisions."""
    pairs = set()
    for f in _collision_findings(pset, {"named_set": True, "hubs": hubs, "root": root, "outside": {}}):
        other, path = f["counterpart_plan"], f["path"]
        if f["class"] == COLLISION and other and path and other in pset and f["plan"] in pset:
            earlier, later = sorted((f["plan"], other))
            pairs.add((later, earlier, path))
    rank = {n: i for i, n in enumerate(sorted({t[1] for t in pairs}, key=lambda n: [-ord(ch) for ch in n]))}
    return sorted(pairs, key=lambda t: (t[0], rank[t[1]], t[2]))


def _plan_edges(pset: Dict[str, _Plan]) -> Dict[str, set]:
    return {rel: {d for d in p.edge_plans() if d in pset} for rel, p in pset.items()}


def _write_edges(root: Path, rel: str, edges: List[dict]) -> bool:
    """Append ``edges`` ({row, edge}) to their rows' ``depends_on_plan`` under one lock.
    True when the file changed."""
    from coordinator_core.locked_write import locked_rmw
    from coordinator_core.ops.plan_tasks_mutate import (
        _carry_prep_certificate,
        _parse_rows_or_abort,
        _patch_body,
        _uses_crlf,
        _validate_all,
        is_governed_plan,
        parse_frontmatter,
    )

    path = root / rel
    crlf = _uses_crlf(path)

    def mutate(old: str) -> str:
        located = locate_fenced_block(old)
        if located.status is not LocateStatus.LOCATED:
            raise MutateAbort(f"{rel}: task spine is {located.status.value}")
        rows = _parse_rows_or_abort(located.body, "seam_fix")
        original = copy.deepcopy(rows)
        by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
        for item in edges:
            row = by_id.get(item["row"])
            if row is None:
                raise MutateAbort(f"{rel}: row {item['row']!r} vanished before the write")
            current = row.get("depends_on_plan")
            current = list(current) if isinstance(current, list) else []
            if item["edge"] not in current:
                current.append(item["edge"])
            row["depends_on_plan"] = current
        fm = parse_frontmatter(old).get("frontmatter")
        _validate_all(
            rows,
            governed=is_governed_plan(fm) if isinstance(fm, dict) else False,
            touched_ids={i["row"] for i in edges},
            plan_created=fm.get("created") if isinstance(fm, dict) else None,
        )
        start, end = located.span
        new = old[:start] + _patch_body(located.body, original, rows) + old[end:]
        new = _carry_prep_certificate(old, new)
        return new.replace("\n", "\r\n") if crlf and new != old else new

    before = path.read_bytes()
    locked_rmw(path, mutate, repo_root=root)
    return path.read_bytes() != before


class EdgeDerivation(NamedTuple):
    """Result of ``derive_edges``; pure data, nothing written."""

    pairs: List[Tuple[str, str, str]]
    graph: Dict[str, set]
    edges_added: List[dict]
    per_plan: Dict[str, List[dict]]
    skipped_cycles: List[dict]
    already_ordered: List[Tuple[str, str, str]]


def _reach_masks(graph: Dict[str, set], bit: Dict[str, int]) -> Dict[str, int]:
    """Per plan, a bitmask of every plan it reaches through one or more ``graph`` edges."""
    reach: Dict[str, int] = {}
    for rel in graph:
        seen = 0
        stack = list(graph[rel])
        while stack:
            cur = stack.pop()
            if not seen & bit[cur]:
                seen |= bit[cur]
                stack.extend(graph.get(cur, ()))
        reach[rel] = seen
    return reach


def derive_edges(pset: Dict[str, _Plan], hubs: set, root: Path) -> EdgeDerivation:
    """The one write-overlap ordering derivation: opens no file, spawns nothing.
    ``graph`` is the in-set plan graph after the added edges."""
    pairs = _collision_pairs(pset, hubs, root)

    graph = _plan_edges(pset)
    bit = {rel: 1 << i for i, rel in enumerate(graph)}
    # Invariant: reach[x] is exactly the in-set plans x transitively depends on, after every
    # edge added to ``graph`` so far.
    reach = _reach_masks(graph, bit)
    orders: Dict[str, List[str]] = {}
    edges_added: List[dict] = []
    skipped: List[dict] = []
    per_plan: Dict[str, List[dict]] = {}
    already_ordered: List[Tuple[str, str, str]] = []
    for later, earlier, path in pairs:
        if reach[later] & bit[earlier]:
            already_ordered.append((later, earlier, path))
            continue
        if reach[earlier] & bit[later]:
            skipped.append({
                "class": CYCLE_CLASS, "plans": [later, earlier], "path": path,
                "detail": f"{later} after {earlier} would close a cycle: {earlier} already "
                          f"reaches {later} through depends_on_plan edges; no edge written",
            })
            continue
        for rel in (later, earlier):
            if rel not in orders:
                orders[rel] = _topo_ids(pset[rel])
        mine = _writer_rows(pset[later], orders[later], path)
        theirs = _writer_rows(pset[earlier], orders[earlier], path)
        if not mine or not theirs:
            continue
        edge = {"plan": earlier, "chunk": theirs[-1], "gate_kind": GATE_KIND,
                "note": f"orders the shared write to {path}"}
        graph[later].add(earlier)
        gained = reach[earlier] | bit[earlier]
        later_bit = bit[later]
        for rel, mask in reach.items():
            if rel == later or mask & later_bit:
                reach[rel] = mask | gained
        for rid in mine:
            per_plan.setdefault(later, []).append({"row": rid, "edge": edge})
            edges_added.append({"plan": later, "chunk": rid,
                                "depends_on": {"plan": earlier, "chunk": theirs[-1]}, "path": path})
    return EdgeDerivation(pairs, graph, edges_added, per_plan, skipped, already_ordered)


def _fix(params: dict, root: Path) -> dict:
    plans, dry_run = _parse_params(params, root)
    pset: Dict[str, _Plan] = {rel: _Plan(rel, root) for rel in plans}
    hubs = _declared_append_hubs(root)
    derived = derive_edges(pset, hubs, root)
    pairs, graph, per_plan = derived.pairs, derived.graph, derived.per_plan
    edges_added, skipped = derived.edges_added, derived.skipped_cycles

    files: List[str] = []
    skipped_plans: List[dict] = []
    if not dry_run:
        failed: Dict[str, str] = {}
        for rel in sorted(per_plan):
            try:
                if _write_edges(root, rel, per_plan[rel]):
                    files.append(rel)
            except MutateAbort as exc:
                failed[rel] = str(exc)
        if failed:
            edges_added = [e for e in edges_added if e["plan"] not in failed]
            skipped_plans = [{"plan": rel, "detail": detail} for rel, detail in failed.items()]
        fresh = {rel: _Plan(rel, root) for rel in plans}
        converged = not _collision_pairs(fresh, hubs, root)
    else:
        converged = all(
            _reaches(graph, a, b) or _reaches(graph, b, a) for a, b, _ in pairs
        )
    return {"edges_added": edges_added, "skipped_cycles": skipped,
            "skipped_plans": skipped_plans, "files": files, "converged": converged}


@register_op("plan.seam_fix")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.seam_fix" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.seam_fix requires a resolved repo_root")
    return _fix(params, Path(main_worktree_root(repo_root)))
