"""plan_seam_fix.derive_edges: derivation cost at 406 certified plans, and parity with the per-pair
DFS derivation it replaced. Plans are synthetic files under tmp_path; git is never spawned.

The oracle below is the pre-bitset derivation copied verbatim and is never imported by product code.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_seam_fix as fix
from coordinator_core.ops.plan_seam_check import _Plan, _collision_findings, COLLISION
from coordinator_core.ops.plan_seam_fix import EdgeDerivation

pytestmark = [pytest.mark.cadence]

STAMP = 'mise_prepped_sha: "abc123"\n'
CHAIN = 320
CLUSTER = 6
PLANS = 406
MIN_PAIRS = 30000


def _row(rid: str, writes: List[str]) -> str:
    return f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n  writes: [{', '.join(writes)}]\n"


def _write_plan(root: Path, rel: str, rows: str, fm: str = STAMP) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntitle: t\nstatus: approved\n{fm}---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8", newline="\n",
    )


def _fm_edge(plan_rel: str) -> str:
    return f"depends_on_plan:\n  - {{plan: {plan_rel}, status: approved, gate_kind: output-consumption-runtime}}\n"


def _parse(root: Path, rels: List[str]) -> Dict[str, _Plan]:
    pset = {rel: _Plan(rel, root) for rel in rels}
    for p in pset.values():
        p.live_rows
    return pset


@pytest.fixture
def corpus(tmp_path):
    """406 plans: one 320-plan chain on a hub path (51040 pairs, deep reachability; the per-pair
    DFS alone costs over 0.5s), the rest in small clusters. Pair density, not edge count, is
    what the cost being cut scales with."""
    rels = []
    for i in range(PLANS):
        rel = f"docs/plans/2026-10-19-p{i:04d}.md"
        if i < CHAIN:
            shared = "shared/hub.py"
        else:
            shared = f"grp{(i - CHAIN) // CLUSTER}/f.py"
        rows = _row("R1", [shared, f"own/{i}.py"]) + _row("R2", [shared])
        _write_plan(tmp_path, rel, rows)
        rels.append(rel)
    return tmp_path, _parse(tmp_path, rels)


def _oracle_pairs(pset, hubs, root) -> List[Tuple[str, str, str]]:
    pairs = set()
    for f in _collision_findings(pset, {"named_set": True, "hubs": hubs, "root": root, "outside": {}}):
        other, path = f["counterpart_plan"], f["path"]
        if f["class"] == COLLISION and other and path and other in pset and f["plan"] in pset:
            earlier, later = sorted((f["plan"], other))
            pairs.add((later, earlier, path))
    return sorted(pairs, key=lambda t: (t[0], [-ord(ch) for ch in t[1]], t[2]))


def _oracle_derive(pset, hubs, root) -> EdgeDerivation:
    pairs = _oracle_pairs(pset, hubs, root)

    graph = fix._plan_edges(pset)
    orders: Dict[str, List[str]] = {}
    edges_added: List[dict] = []
    skipped: List[dict] = []
    per_plan: Dict[str, List[dict]] = {}
    already_ordered: List[Tuple[str, str, str]] = []
    for later, earlier, path in pairs:
        if fix._reaches(graph, later, earlier):
            already_ordered.append((later, earlier, path))
            continue
        if fix._reaches(graph, earlier, later):
            skipped.append({
                "class": fix.CYCLE_CLASS, "plans": [later, earlier], "path": path,
                "detail": f"{later} after {earlier} would close a cycle: {earlier} already "
                          f"reaches {later} through depends_on_plan edges; no edge written",
            })
            continue
        for rel in (later, earlier):
            if rel not in orders:
                orders[rel] = fix._topo_ids(pset[rel])
        mine = fix._writer_rows(pset[later], orders[later], path)
        theirs = fix._writer_rows(pset[earlier], orders[earlier], path)
        if not mine or not theirs:
            continue
        edge = {"plan": earlier, "chunk": theirs[-1], "gate_kind": fix.GATE_KIND,
                "note": f"orders the shared write to {path}"}
        graph[later].add(earlier)
        for rid in mine:
            per_plan.setdefault(later, []).append({"row": rid, "edge": edge})
            edges_added.append({"plan": later, "chunk": rid,
                                "depends_on": {"plan": earlier, "chunk": theirs[-1]}, "path": path})
    return EdgeDerivation(pairs, graph, edges_added, per_plan, skipped, already_ordered)


def test_derive_edges_first_call_under_budget_at_406_plans(corpus, monkeypatch):
    root, pset = corpus
    assert len(pset) == PLANS
    start = time.process_time()
    with _count_spawns_attributed(monkeypatch) as spawns:
        derived = fix.derive_edges(pset, set(), root)
    elapsed = time.process_time() - start
    assert len(derived.pairs) >= MIN_PAIRS, len(derived.pairs)
    assert derived.edges_added and not derived.skipped_cycles
    assert list(spawns) == []
    assert elapsed < 0.5, f"derive_edges took {elapsed:.3f}s process time over {len(derived.pairs)} pairs"


def test_derive_edges_equals_the_per_pair_dfs_oracle_at_406_plans(corpus):
    root, pset = corpus
    assert fix.derive_edges(pset, set(), root) == _oracle_derive(pset, set(), root)


def test_derive_edges_equals_the_oracle_on_declared_edges_cycles_and_prefix_names(tmp_path):
    """a(x) declares it depends on d; c(x, z) and d(z): c->a is derived, then d->c would close a
    cycle through the declared a->d edge. p.md is a prefix of p.md.md as earlier-plan names."""
    (tmp_path / ".git").mkdir()
    a, c, d = "docs/plans/a.md", "docs/plans/c.md", "docs/plans/d.md"
    _write_plan(tmp_path, a, _row("R1", ["x.py"]), fm=STAMP + _fm_edge(d))
    _write_plan(tmp_path, c, _row("R1", ["x.py", "z.py"]))
    _write_plan(tmp_path, d, _row("R1", ["z.py"]))
    pre, pre_long, tail = "docs/plans/p.md", "docs/plans/p.md.md", "docs/plans/q.md"
    _write_plan(tmp_path, pre, _row("R1", ["m.py"]))
    _write_plan(tmp_path, pre_long, _row("R1", ["m.py"]))
    _write_plan(tmp_path, tail, _row("R1", ["m.py"]) + _row("R2", ["n.py"]))
    e = "docs/plans/e.md"
    _write_plan(tmp_path, e, _row("R1", ["x.py"]), fm=STAMP + _fm_edge(a))
    pset = _parse(tmp_path, [a, c, d, pre, pre_long, tail, e])
    derived = fix.derive_edges(pset, set(), tmp_path)
    assert derived.skipped_cycles, "fixture must carry a mixed declared/derived cycle"
    assert derived.edges_added
    assert derived == _oracle_derive(pset, set(), tmp_path)


BOUND_PLANS = 200
BOUND_CHAIN = 170
BOUND_MIN_EDGES = 158


def test_write_order_first_warm_under_budget_at_the_200_plan_bound(tmp_path, monkeypatch):
    """Fresh files salted with a token unique to this case, so no rows-memo hit from other cases."""
    from coordinator_core.ops import plan_write_order as op

    (tmp_path / ".git").mkdir()
    rels = []
    for i in range(BOUND_PLANS):
        rel = f"docs/plans/2026-10-19-w{i:04d}.md"
        shared = "shared/hub.py" if i < BOUND_CHAIN else f"grp{(i - BOUND_CHAIN) // 5}/f.py"
        rows = _row("R1", [shared, f"own/{i}.py"]) + _row("R2", [shared])
        _write_plan(tmp_path, rel, rows, fm=STAMP + f"salt: c8-bound-{i}-{time.time_ns()}\n")
        rels.append(rel)
    start = time.process_time()
    with _count_spawns_attributed(monkeypatch) as spawns:
        reply = op._write_order({"plans": rels}, tmp_path)
    elapsed = time.process_time() - start
    assert len(reply["set"]) == BOUND_PLANS and not reply["unreadable"]
    assert len(reply["edges"]) >= BOUND_MIN_EDGES, len(reply["edges"])
    assert list(spawns) == []
    assert elapsed < 0.5, f"_write_order took {elapsed:.3f}s process time at {BOUND_PLANS} plans"
