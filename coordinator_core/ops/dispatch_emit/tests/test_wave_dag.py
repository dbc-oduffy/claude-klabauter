"""
Tests for coordinator_core.ops.dispatch_emit.wave_map.dag_from_waves / build_dag.

Spec backlink: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md
§ Design D4, task C3, AC3.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import (
    DagNode,
    DagPlan,
    build_dag,
    build_waves,
    dag_from_waves,
)


def _row(id_, writes, depends_on=None, reads=None):
    return EmitterRow(
        id=id_,
        title=f"title-{id_}",
        surface="test",
        writes=writes,
        reads=reads or [],
        depends_on=depends_on or [],
    )


def test_three_disjoint_rows_width_3_critical_path_1():
    rows = [_row("C1", ["a.py"]), _row("C2", ["b.py"]), _row("C3", ["c.py"])]
    dag = build_dag(rows)
    assert isinstance(dag, DagPlan)
    assert dag.max_concurrent_rows == 3
    assert dag.critical_path_rows == 1
    assert len(dag.nodes) == 3
    for node in dag.nodes:
        assert isinstance(node, DagNode)
        assert node.after == ()


def test_three_row_depends_on_chain_width_1_critical_path_3():
    rows = [
        _row("C1", ["a.py"]),
        _row("C2", ["b.py"], depends_on=[{"chunk": "C1"}]),
        _row("C3", ["c.py"], depends_on=[{"chunk": "C2"}]),
    ]
    dag = build_dag(rows)
    assert dag.max_concurrent_rows == 1
    assert dag.critical_path_rows == 3
    by_id = {node.row.id: node for node in dag.nodes}
    assert by_id["C1"].after == ()
    assert by_id["C2"].after == ("C1",)
    assert by_id["C3"].after == ("C2",)


def test_diamond_width_2_critical_path_3():
    rows = [
        _row("A", ["a.py"]),
        _row("B", ["b.py"], depends_on=[{"chunk": "A"}]),
        _row("C", ["c.py"], depends_on=[{"chunk": "A"}]),
        _row("D", ["d.py"], depends_on=[{"chunk": "B"}, {"chunk": "C"}]),
    ]
    dag = build_dag(rows)
    assert dag.max_concurrent_rows == 2
    assert dag.critical_path_rows == 3
    by_id = {node.row.id: node for node in dag.nodes}
    assert by_id["A"].after == ()
    assert set(by_id["B"].after) == {"A"}
    assert set(by_id["C"].after) == {"A"}
    assert set(by_id["D"].after) == {"B", "C"}


def test_two_write_overlapping_rows_get_one_after_edge_in_build_waves_order():
    rows = [_row("C1", ["shared.py"]), _row("C2", ["shared.py"])]
    waves = build_waves(rows)
    # build_waves forces the overlapping pair into separate, ordered waves.
    flat_order = [w.id for wave in waves for w in wave]
    dag = dag_from_waves(waves)
    by_id = {node.row.id: node for node in dag.nodes}
    earlier, later = flat_order[0], flat_order[1]
    assert by_id[earlier].after == ()
    assert by_id[later].after == (earlier,)
    assert dag.critical_path_rows == 2
    assert dag.max_concurrent_rows == 1


def test_build_waves_fixtures_unchanged_by_dag_addition():
    # build_waves stays the validator/reporter; adding build_dag must not
    # perturb its own output shape or values.
    rows = [_row("C1", ["a.py"]), _row("C2", ["b.py"], depends_on=[{"chunk": "C1"}])]
    before = build_waves(rows)
    dag = build_dag(rows)
    after = build_waves(rows)
    assert before == after
    assert dag.waves == after


def test_dag_from_waves_reuses_predecessors_and_writes_overlap_over_waverow():
    # dag_from_waves must work directly off build_waves' WaveRow output
    # (not require the original EmitterRow list), since _predecessors and
    # _writes_overlap both take any row exposing id/writes/writes_under/
    # reads/depends_on -- WaveRow satisfies that shape.
    rows = [
        _row("C1", ["a.py"]),
        _row("C2", ["b.py"], reads=["a.py"]),
    ]
    waves = build_waves(rows)
    dag = dag_from_waves(waves)
    by_id = {node.row.id: node for node in dag.nodes}
    assert by_id["C2"].after == ("C1",)
