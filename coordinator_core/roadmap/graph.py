"""
coordinator_core.roadmap.graph — graph primitives for roadmap stub topological
linearization.

Purpose: provides a constructive topological linearization of the blocks/blocked_by
DAG (``topo_number``) and a fail-loud verification gate for dependency-monotone
(sprint, wave) ordering (``check_dependency_order``). Backs
``coordinator_core.roadmap.number_stubs`` (authoring CLI, port of
``roadmap-number-stubs.js``).

Domain vocabulary: stub, sprint, wave, topological linearization, blocked_by edge,
dependency-monotone, (sprint, wave) slot.

Port source: coordinator/bin/lib/roadmap-graph.js (example-doctrine-repo), 345 LoC.
Spec backlink: docs/plans/2026-06-28-roadmap-stub-numbering-dependency-order.md § C1
Port backlink: BIG_PORT Wave B, item roadmap-pair.

Negative-spec:
  - This is a FRESH, independent port — NOT a reuse of
    ``coordinator_core.roadmap.audit``'s own inline ``check_dependency_order`` /
    ``_detect_cycles`` (that module ports Audit 5 of ``audit-roadmap.sh`` and landed
    separately under a different plan). The porter brief for this item explicitly
    calls out that the existing ``coordinator_core/roadmap/`` package does not cover
    this item's semantics and instructs a fresh port rather than assuming coverage —
    the resulting near-duplicate of the dependency-order-check logic across the two
    modules is accepted debt, not an oversight; a future consolidation pass (not this
    item) can fold both into a single shared implementation if desired.
  - Does not implement multi-sprint sprint assignment — ``topo_number`` is
    constructive for single-sprint only (``sprint=1`` for every node), exactly per the
    oracle's own docstring; multi-sprint authoring is human-driven and verified
    separately by the dependency-order gate.
  - ``topo_number``'s ``sprintWave`` wave assignment is NOT byte-parity with
    roadmap-graph.js's flat ``wave=depth+1`` (only ``order``/``number`` are).
    The oracle's flat bucketing collapses every same-depth sibling into one
    shared wave, which fails ``coordinator_core.roadmap.audit``'s Audit 2 (at
    most one ready_to_fire stub per (sprint, wave)) the moment two same-depth
    stubs are both marked ready_to_fire — a live, observed contradiction
    between this package's own two ops, not a hypothetical. Fixed by spreading
    same-depth siblings across distinct waves (see ``topo_number``'s
    docstring); this is an intentional correction of the oracle, not drift.
"""

from __future__ import annotations

import re
from functools import cmp_to_key
from typing import Any, Callable, Dict, List, Optional

_TRAILING_NUM_RE = re.compile(r"[-_](\d+)$")


class RoadmapCycleError(Exception):
    """Raised by ``topo_number`` when the blocked_by graph contains a cycle.

    ``.cycle`` carries the list of node labels that form the detected cycle set.
    Cycles are authoring bugs to surface, never to linearize around — mirrors the
    oracle's ``RoadmapCycleError`` class (roadmap-graph.js).
    """

    def __init__(self, cycle: List[str]) -> None:
        super().__init__(f"Roadmap dependency cycle detected among nodes: {', '.join(cycle)}")
        self.cycle = cycle


# ---------------------------------------------------------------------------
# topo_number — constructive topological linearization (Kahn's algorithm)
# ---------------------------------------------------------------------------


def topo_number(
    nodes: List[str],
    edges: List[Dict[str, str]],
    tie_break: Optional[Callable[[str, str], int]] = None,
) -> Dict[str, Any]:
    """Produce a dependency-monotone topological linearization of a stub DAG.

    ``order``/``number`` are a byte-parity port of roadmap-graph.js's
    ``topoNumber``. ``sprintWave`` deliberately DIVERGES from the oracle's flat
    ``wave=depth+1`` -- see § wave assignment below and the module docstring's
    Negative-spec for why.

    Wave assignment: single-sprint (sprint=1 for every node). Wave is NOT the
    raw longest-path depth bucket -- same-depth siblings are spread across
    distinct, sequential waves (sorted by (depth, tie_break), reusing the same
    ``cmp`` comparator used to order ``order``) rather than collapsed into one
    shared wave. Depth strictly increases along every blocked_by edge by
    construction, so this spreading preserves strict (sprint, wave) monotonicity
    along every edge while also giving every node a unique wave -- the flat
    depth+1 assignment gave every same-depth sibling an IDENTICAL wave, which
    collides with ``coordinator_core.roadmap.audit``'s Audit 2 (at most one
    ready_to_fire stub per (sprint, wave)).

    Tie-breaking within a ready-set (same Kahn's layer): the optional ``tie_break``
    comparator is applied; default is stable original nodes-array index order.
    The same comparator also breaks ties within a depth bucket for wave
    assignment (see above).

    Args:
        nodes: provisional stub cluster labels.
        edges: each entry ``{"from": ..., "to": ...}`` encodes "from blocked_by to"
            -- 'to' must ship first and therefore receives the lower number. The
            precedence arrow in the DAG is to -> from.
        tie_break: optional comparator ``(a, b) -> int`` for deterministic
            tie-breaking within a ready set. Default: stable original
            nodes-array index order.

    Returns:
        ``{"order": [...], "number": {...}, "sprintWave": {...}}`` --
        order: labels in topological linearization order (dependencies before
        dependents); number: 1-based position of each label; sprintWave: per-label
        ``{"sprint": 1, "wave": <unique per-node, depth-monotone>}``.

    Raises:
        RoadmapCycleError: if the blocked_by graph contains a cycle.
    """
    node_index = {n: i for i, n in enumerate(nodes)}

    def default_tie_break(a: str, b: str) -> int:
        return node_index[a] - node_index[b]

    cmp = tie_break or default_tie_break

    in_degree: Dict[str, int] = {}
    successors: Dict[str, List[str]] = {}

    for n in nodes:
        in_degree[n] = 0
        successors[n] = []

    for edge in edges:
        frm = edge["from"]
        to = edge["to"]
        # Guard: prevents a KeyError during in_degree/successors bookkeeping
        # for a label that appears in edges but not in `nodes` — does NOT add
        # the node to the output (`ready`/`order` are seeded from `nodes`
        # only). Callers must ensure `nodes` is the full label set; this
        # module's only caller (number_stubs.py's derive_nodes) always
        # unions from/to into the node set before calling topo_number.
        # Review: code-reviewer — F5, comment overstated what the guard does.
        if frm not in in_degree:
            in_degree[frm] = 0
        if to not in successors:
            successors[to] = []
        in_degree[frm] = in_degree.get(frm, 0) + 1
        successors[to].append(frm)

    ready: List[str] = [n for n in nodes if in_degree.get(n, 0) == 0]

    order: List[str] = []
    depth: Dict[str, int] = {n: 0 for n in nodes}

    while ready:
        # Deterministic selection: sort the ready set, take the first element.
        ready.sort(key=cmp_to_key(cmp))
        node = ready.pop(0)
        order.append(node)

        for succ in successors.get(node, []):
            candidate = depth.get(node, 0) + 1
            if candidate > depth.get(succ, 0):
                depth[succ] = candidate
            new_deg = in_degree.get(succ, 0) - 1
            in_degree[succ] = new_deg
            if new_deg == 0:
                ready.append(succ)

    if len(order) < len(nodes):
        consumed = set(order)
        cycle = [n for n in nodes if n not in consumed]
        raise RoadmapCycleError(cycle)

    number: Dict[str, int] = {}
    for i, label in enumerate(order):
        number[label] = i + 1

    # Wave assignment: NOT flat depth+1 (that bucket-collapses every same-depth
    # sibling into one wave, which collides with audit.py's Audit 2 — at most
    # one ready_to_fire stub per (sprint, wave)). Instead, spread same-depth
    # siblings across distinct, sequential waves while preserving strict
    # monotonicity along every edge: sort ALL nodes by (depth, tie_break) --
    # reusing the same `cmp` comparator `order` was built with, not a second
    # parallel ordering algorithm -- then assign wave = position + 1. Because
    # depth strictly increases along every blocked_by edge (see the Kahn's-loop
    # `candidate = depth[node] + 1` update above), a dependency always sorts
    # before its dependent here, so wave(dependency) < wave(dependent) holds
    # for every edge (Audit 5) while every node still gets a unique wave
    # (Audit 2). This is a deliberate divergence from roadmap-graph.js's flat
    # depth+1 wave -- see the module docstring's Negative-spec.
    def wave_cmp(a: str, b: str) -> int:
        da, db = depth.get(a, 0), depth.get(b, 0)
        if da != db:
            return da - db
        return cmp(a, b)

    wave_order = sorted(nodes, key=cmp_to_key(wave_cmp))
    sprint_wave: Dict[str, Dict[str, int]] = {}
    for i, label in enumerate(wave_order):
        sprint_wave[label] = {"sprint": 1, "wave": i + 1}

    return {"order": order, "number": number, "sprintWave": sprint_wave}


# ---------------------------------------------------------------------------
# check_dependency_order — fail-loud verification gate for authored stubs
# ---------------------------------------------------------------------------


def _resolve_number(stub: Dict[str, Any]) -> Optional[int]:
    num = stub.get("number")
    if num is not None:
        return num
    m = _TRAILING_NUM_RE.search(str(stub.get("stub_id", "")))
    return int(m.group(1)) if m else None


def check_dependency_order(stubs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Verify stub numbers and (sprint, wave) slots are dependency-monotone across
    every declared blocked_by edge in *stubs*.

    Byte-parity port of roadmap-graph.js's ``checkDependencyOrder`` -- see that
    file's docstring for the full invariant description (repeated in brief here):

    Invariant (must hold for every edge A blocked_by B, B ships first)::

        number(B) < number(A)                            [strict]
        (sprint(B), wave(B)) <_lex (sprint(A), wave(A))   [strict; equal tuples
                                                             are violations]

    Missing sprint on either endpoint of a dependency edge is a fail-loud violation
    of its own class (``missing-sprint``) -- not silently coerced into a comparison.
    Edges pointing to stub_ids outside the provided set are reported as
    ``unresolved`` (not silently dropped). All violations are collected without
    short-circuiting.

    Args:
        stubs: list of stub descriptors --
            ``{"stub_id": str, "number"?: int, "sprint"?: int, "wave"?: int,
            "blocked_by"?: list[str]}``. If ``number`` is absent, it is derived
            by parsing the trailing ``-<N>`` integer from ``stub_id``.

    Returns:
        ``{"ok": bool, "violations": [...], "unresolved": [...], "cycle": list|None}``.
    """
    violations: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    stub_map: Dict[str, Dict[str, Any]] = {}
    for stub in stubs:
        entry = dict(stub)
        entry["_num"] = _resolve_number(stub)
        stub_map[stub["stub_id"]] = entry

    for stub in stubs:
        a = stub_map[stub["stub_id"]]
        blocked_by = stub.get("blocked_by") or []

        for dep_id in blocked_by:
            if dep_id not in stub_map:
                unresolved.append(
                    {"from": stub["stub_id"], "to": dep_id, "reason": "unresolved-edge"}
                )
                continue

            b = stub_map[dep_id]

            a_missing_s = a.get("sprint") is None
            b_missing_s = b.get("sprint") is None
            if a_missing_s or b_missing_s:
                violations.append(
                    {
                        "from": a["stub_id"],
                        "to": b["stub_id"],
                        "reason": "missing-sprint",
                        "which": "both"
                        if (a_missing_s and b_missing_s)
                        else ("A" if a_missing_s else "B"),
                    }
                )
                continue

            if a["_num"] is not None and b["_num"] is not None:
                if not (b["_num"] < a["_num"]):
                    violations.append(
                        {
                            "from": a["stub_id"],
                            "to": b["stub_id"],
                            "reason": "number-order",
                            "numberA": a["_num"],
                            "numberB": b["_num"],
                        }
                    )

            a_has_w = a.get("wave") is not None
            b_has_w = b.get("wave") is not None
            if a_has_w and b_has_w:
                slot_ok = b["sprint"] < a["sprint"] or (
                    b["sprint"] == a["sprint"] and b["wave"] < a["wave"]
                )
                if not slot_ok:
                    violations.append(
                        {
                            "from": a["stub_id"],
                            "to": b["stub_id"],
                            "reason": "same-or-inverted-slot",
                            "slotA": {"sprint": a["sprint"], "wave": a["wave"]},
                            "slotB": {"sprint": b["sprint"], "wave": b["wave"]},
                        }
                    )

    cycle = _detect_cycles(stubs, stub_map)

    ok = not violations and not unresolved and cycle is None
    return {"ok": ok, "violations": violations, "unresolved": unresolved, "cycle": cycle}


# ---------------------------------------------------------------------------
# Internal: DFS cycle detection
# ---------------------------------------------------------------------------


def _detect_cycles(
    stubs: List[Dict[str, Any]], stub_map: Dict[str, Dict[str, Any]]
) -> Optional[List[str]]:
    """Detect cycles in the blocked_by graph using recursive DFS with three-colour
    marking. Only traverses edges where both endpoints are in *stub_map* (the
    provided set). Returns the cycle's stub_ids, or ``None`` if acyclic. Byte-parity
    port of roadmap-graph.js's ``_detectCycles``.
    """
    white, gray, black = 0, 1, 2
    color: Dict[str, int] = {stub["stub_id"]: white for stub in stubs}
    cycle_found: Optional[List[str]] = None

    def dfs(node_id: str, path: List[str]) -> None:
        nonlocal cycle_found
        if cycle_found is not None:
            return
        color[node_id] = gray
        path.append(node_id)

        stub = stub_map[node_id]
        for dep_id in stub.get("blocked_by") or []:
            if dep_id not in stub_map:
                continue
            if cycle_found is not None:
                return
            c = color.get(dep_id)
            if c == gray:
                cycle_start = path.index(dep_id)
                cycle_found = list(path[cycle_start:])
                return
            if c == white:
                dfs(dep_id, path)

        path.pop()
        color[node_id] = black

    for stub in stubs:
        if cycle_found is not None:
            break
        if color.get(stub["stub_id"]) == white:
            dfs(stub["stub_id"], [])

    return cycle_found
