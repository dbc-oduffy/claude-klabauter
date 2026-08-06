"""
coordinator_core.roadmap.tests.test_graph — independent parity regression net for
coordinator_core.roadmap.graph, re-derived from the acceptance criteria (AC1-AC3,
plus the F0/F1/F2 checkDependencyOrder cases) documented in the bash/JS oracle's own
test file (coordinator/bin/tests/test-roadmap-graph.js, example-doctrine-repo) rather than by
re-asserting this port's own transcription — each case below was independently
re-run against the oracle (``node coordinator/bin/lib/roadmap-graph.js``) to confirm
the expected values before being hardcoded here.

Spec backlink: docs/plans/2026-06-28-roadmap-stub-numbering-dependency-order.md § C6
Port backlink: BIG_PORT Wave B, item roadmap-pair.
"""

from __future__ import annotations

import pytest

from coordinator_core.roadmap.graph import (
    RoadmapCycleError,
    check_dependency_order,
    topo_number,
)

# ---------------------------------------------------------------------------
# AC1 — topo-number-respects-dependency-order
# ---------------------------------------------------------------------------


def test_topo_number_respects_dependency_order_every_edge():
    # DAG: B is a root; A blocked_by B; C blocked_by B; D blocked_by A and C.
    nodes = ["A", "B", "C", "D"]
    edges = [
        {"from": "A", "to": "B"},
        {"from": "C", "to": "B"},
        {"from": "D", "to": "A"},
        {"from": "D", "to": "C"},
    ]

    result = topo_number(nodes, edges)

    assert isinstance(result["order"], list)
    assert isinstance(result["number"], dict)
    assert isinstance(result["sprintWave"], dict)

    for edge in edges:
        num_dep = result["number"][edge["to"]]
        num_dpnd = result["number"][edge["from"]]
        assert num_dep < num_dpnd

    # order/number: independently re-derived from the oracle run:
    # node coordinator/bin/lib/roadmap-graph.js
    assert result["order"] == ["B", "A", "C", "D"]
    assert result["number"] == {"B": 1, "A": 2, "C": 3, "D": 4}
    # sprintWave: this port DELIBERATELY diverges from the oracle here (see
    # topo_number's docstring + graph.py's module Negative-spec) — the oracle's
    # flat wave=depth+1 would give A and C (same depth) the identical wave 2,
    # which fails audit.py's Audit 2 uniqueness gate. Same-depth siblings are
    # spread across distinct waves instead, tie-broken by the same `cmp` used
    # to order `order` (here: default nodes-array index, A before C).
    assert result["sprintWave"] == {
        "B": {"sprint": 1, "wave": 1},
        "A": {"sprint": 1, "wave": 2},
        "C": {"sprint": 1, "wave": 3},
        "D": {"sprint": 1, "wave": 4},
    }


def test_topo_number_deterministic_across_repeated_calls():
    nodes = ["A", "B", "C", "D"]
    edges = [
        {"from": "A", "to": "B"},
        {"from": "C", "to": "B"},
        {"from": "D", "to": "A"},
        {"from": "D", "to": "C"},
    ]
    r1 = topo_number(nodes, edges)
    r2 = topo_number(nodes, edges)
    assert r1["order"] == r2["order"]
    assert r1["number"] == r2["number"]


def test_topo_number_tie_break_by_stable_nodes_array_index():
    # A and C are both unblocked once B is placed (same Kahn layer). Default
    # tie-break uses original nodes-array index: A (index 0) before C (index 2).
    nodes = ["A", "B", "C", "D"]
    edges = [
        {"from": "A", "to": "B"},
        {"from": "C", "to": "B"},
        {"from": "D", "to": "A"},
        {"from": "D", "to": "C"},
    ]
    result = topo_number(nodes, edges)
    pos_a = result["order"].index("A")
    pos_c = result["order"].index("C")
    assert pos_a < pos_c


# ---------------------------------------------------------------------------
# AC2 — wave-is-sprint-wave-monotone
# ---------------------------------------------------------------------------


def test_sprint_wave_strictly_lexicographically_increasing_along_edges():
    nodes = ["A", "B", "C"]
    edges = [{"from": "B", "to": "A"}, {"from": "C", "to": "B"}]
    result = topo_number(nodes, edges)

    for edge in edges:
        sw_dep = result["sprintWave"][edge["to"]]
        sw_dpnd = result["sprintWave"][edge["from"]]
        dep_lex = sw_dep["sprint"] * 10000 + sw_dep["wave"]
        dpnd_lex = sw_dpnd["sprint"] * 10000 + sw_dpnd["wave"]
        assert dep_lex < dpnd_lex

    # Independently re-derived from the oracle run.
    assert result["order"] == ["A", "B", "C"]
    assert result["sprintWave"] == {
        "A": {"sprint": 1, "wave": 1},
        "B": {"sprint": 1, "wave": 2},
        "C": {"sprint": 1, "wave": 3},
    }


def test_single_sprint_assigns_sprint_1_to_all_nodes():
    nodes = ["A", "B", "C"]
    edges = [{"from": "B", "to": "A"}, {"from": "C", "to": "B"}]
    result = topo_number(nodes, edges)
    for label in nodes:
        assert result["sprintWave"][label]["sprint"] == 1


def test_wave_depth_increases_along_dependency_chain():
    nodes = ["A", "B", "C"]
    edges = [{"from": "B", "to": "A"}, {"from": "C", "to": "B"}]
    result = topo_number(nodes, edges)
    assert result["sprintWave"]["A"]["wave"] == 1
    assert result["sprintWave"]["B"]["wave"] == 2
    assert result["sprintWave"]["C"]["wave"] == 3


def test_same_depth_siblings_get_distinct_waves_no_audit2_collision():
    # A and C are same-depth siblings (both blocked_by B only) — the flat
    # depth+1 assignment would collide them onto wave 2, which fails
    # audit.py's Audit 2 (at most one ready_to_fire stub per (sprint, wave))
    # the moment both are marked ready_to_fire. Regression pin for that fix.
    nodes = ["A", "B", "C", "D"]
    edges = [
        {"from": "A", "to": "B"},
        {"from": "C", "to": "B"},
        {"from": "D", "to": "A"},
        {"from": "D", "to": "C"},
    ]
    result = topo_number(nodes, edges)
    waves = [result["sprintWave"][label]["wave"] for label in nodes]
    assert len(waves) == len(set(waves)), (
        f"Audit-2-forbidden wave collision among nodes with no direct blocked_by "
        f"edge between them: {result['sprintWave']}"
    )


def test_wave_monotone_non_decreasing_with_depth_audit5():
    # Audit 5 (checked via check_dependency_order, exercised elsewhere in this
    # module) requires wave to be strictly increasing along a declared edge;
    # this pins the weaker, always-true structural invariant the fix must not
    # break: wave never decreases as longest-path depth increases.
    nodes = ["A", "B", "C", "D", "E"]
    edges = [
        {"from": "A", "to": "B"},
        {"from": "C", "to": "B"},
        {"from": "D", "to": "A"},
        {"from": "D", "to": "C"},
        {"from": "E", "to": "B"},
    ]
    result = topo_number(nodes, edges)
    # Recompute depth independently via number/order relationship is not
    # possible from the public return value alone, so assert the concrete
    # depth-derived expectation directly: B is the sole root (depth 0); A, C, E
    # are depth 1 (share no edge among themselves); D is depth 2.
    depth_by_label = {"B": 0, "A": 1, "C": 1, "E": 1, "D": 2}
    for edge in edges:
        dep, dpnd = edge["to"], edge["from"]
        assert depth_by_label[dpnd] >= depth_by_label[dep]
        assert result["sprintWave"][dpnd]["wave"] > result["sprintWave"][dep]["wave"]
    # And every node still gets a unique wave slot.
    waves = [result["sprintWave"][label]["wave"] for label in nodes]
    assert len(waves) == len(set(waves))


def test_same_slot_dependency_is_a_slot_violation():
    stubs = [
        {"stub_id": "stub-1", "number": 1, "sprint": 1, "wave": 1},
        {"stub_id": "stub-2", "number": 2, "sprint": 1, "wave": 1, "blocked_by": ["stub-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    assert any(v["reason"] == "same-or-inverted-slot" for v in result["violations"])


# ---------------------------------------------------------------------------
# AC3 — cycle-fails-loud
# ---------------------------------------------------------------------------


def test_topo_number_raises_roadmap_cycle_error_on_cyclic_dag():
    nodes = ["X", "Y", "Z"]
    edges = [
        {"from": "X", "to": "Y"},
        {"from": "Y", "to": "Z"},
        {"from": "Z", "to": "X"},
    ]
    with pytest.raises(RoadmapCycleError):
        topo_number(nodes, edges)


def test_roadmap_cycle_error_carries_nonempty_cycle_list():
    nodes = ["X", "Y"]
    edges = [{"from": "X", "to": "Y"}, {"from": "Y", "to": "X"}]

    with pytest.raises(RoadmapCycleError) as excinfo:
        topo_number(nodes, edges)

    cycle = excinfo.value.cycle
    assert isinstance(cycle, list) and len(cycle) > 0
    assert "X" in cycle and "Y" in cycle


def test_check_dependency_order_returns_nonnull_cycle_for_cyclic_stub_set():
    stubs = [
        {"stub_id": "alpha-1", "number": 1, "sprint": 1, "wave": 1, "blocked_by": ["beta-2"]},
        {"stub_id": "beta-2", "number": 2, "sprint": 1, "wave": 2, "blocked_by": ["alpha-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["cycle"] is not None
    assert isinstance(result["cycle"], list) and len(result["cycle"]) > 0
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# checkDependencyOrder — number-order violation
# ---------------------------------------------------------------------------


def test_number_order_inverted_produces_violation():
    stubs = [
        {"stub_id": "stub-a-2", "number": 2, "sprint": 1, "wave": 2, "blocked_by": ["stub-b-3"]},
        {"stub_id": "stub-b-3", "number": 3, "sprint": 1, "wave": 3},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    v = next(v for v in result["violations"] if v["reason"] == "number-order")
    assert isinstance(v["numberA"], int)
    assert isinstance(v["numberB"], int)
    assert v["from"] == "stub-a-2"
    assert v["to"] == "stub-b-3"


def test_number_order_derived_from_stub_id_trailing_number():
    stubs = [
        {"stub_id": "stub-a-2", "sprint": 1, "wave": 2, "blocked_by": ["stub-b-4"]},
        {"stub_id": "stub-b-4", "sprint": 1, "wave": 1},
    ]
    result = check_dependency_order(stubs)
    assert any(v["reason"] == "number-order" for v in result["violations"])


# ---------------------------------------------------------------------------
# checkDependencyOrder — unresolved edge
# ---------------------------------------------------------------------------


def test_unresolved_edge_produces_entry():
    stubs = [{"stub_id": "present-1", "sprint": 1, "wave": 2, "blocked_by": ["ghost-id"]}]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    assert len(result["unresolved"]) == 1
    u = result["unresolved"][0]
    assert u["from"] == "present-1"
    assert u["to"] == "ghost-id"
    assert u["reason"] == "unresolved-edge"


# ---------------------------------------------------------------------------
# checkDependencyOrder — same-or-inverted-slot violation
# ---------------------------------------------------------------------------


def test_same_or_inverted_slot_same_slot():
    stubs = [
        {"stub_id": "dep-1", "number": 1, "sprint": 2, "wave": 3},
        {"stub_id": "dpnd-2", "number": 2, "sprint": 2, "wave": 3, "blocked_by": ["dep-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    v = next(v for v in result["violations"] if v["reason"] == "same-or-inverted-slot")
    assert v["slotA"] == {"sprint": 2, "wave": 3}
    assert v["slotB"] == {"sprint": 2, "wave": 3}


def test_same_or_inverted_slot_inverted():
    stubs = [
        {"stub_id": "dep-1", "number": 1, "sprint": 2, "wave": 1},
        {"stub_id": "dpnd-2", "number": 2, "sprint": 1, "wave": 1, "blocked_by": ["dep-1"]},
    ]
    result = check_dependency_order(stubs)
    assert any(v["reason"] == "same-or-inverted-slot" for v in result["violations"])


# ---------------------------------------------------------------------------
# checkDependencyOrder — missing-sprint violation
# ---------------------------------------------------------------------------


def test_missing_sprint_on_dependent():
    stubs = [
        {"stub_id": "dep-b-1", "number": 1, "sprint": 1, "wave": 1},
        {"stub_id": "dpnd-a-2", "number": 2, "wave": 2, "blocked_by": ["dep-b-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    v = next(v for v in result["violations"] if v["reason"] == "missing-sprint")
    assert v["which"] == "A"


def test_missing_sprint_on_dependency():
    stubs = [
        {"stub_id": "dep-b-1", "number": 1, "wave": 1},
        {"stub_id": "dpnd-a-2", "number": 2, "sprint": 1, "wave": 2, "blocked_by": ["dep-b-1"]},
    ]
    result = check_dependency_order(stubs)
    v = next(v for v in result["violations"] if v["reason"] == "missing-sprint")
    assert v["which"] == "B"


def test_missing_sprint_is_own_class_no_fallthrough():
    stubs = [
        {"stub_id": "dep-b-1", "number": 1},
        {"stub_id": "dpnd-a-2", "number": 2, "sprint": 1, "wave": 1, "blocked_by": ["dep-b-1"]},
    ]
    result = check_dependency_order(stubs)
    missing_sprint = [v for v in result["violations"] if v["reason"] == "missing-sprint"]
    others = [v for v in result["violations"] if v["reason"] != "missing-sprint"]
    assert len(missing_sprint) == 1
    assert len(others) == 0


# ---------------------------------------------------------------------------
# checkDependencyOrder — clean result
# ---------------------------------------------------------------------------


def test_ok_true_when_all_edges_dependency_monotone():
    stubs = [
        {"stub_id": "a-1", "number": 1, "sprint": 1, "wave": 1},
        {"stub_id": "b-2", "number": 2, "sprint": 1, "wave": 2, "blocked_by": ["a-1"]},
        {"stub_id": "c-3", "number": 3, "sprint": 2, "wave": 1, "blocked_by": ["b-2"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is True
    assert result["violations"] == []
    assert result["unresolved"] == []
    assert result["cycle"] is None
