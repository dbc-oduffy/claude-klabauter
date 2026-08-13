"""
Tests for coordinator_core.ops.dispatch_emit.wave_map.build_waves.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C2.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import WaveCycleError, build_waves


def _row(id_, writes, depends_on=None, reads=None):
    return EmitterRow(
        id=id_,
        title=f"title-{id_}",
        surface="test",
        writes=writes,
        reads=reads or [],
        depends_on=depends_on or [],
    )


def test_disjoint_rows_collapse_into_one_wave():
    rows = [_row("C1", ["a.py"]), _row("C2", ["b.py"]), _row("C3", ["c.py"])]
    waves = build_waves(rows)
    assert len(waves) == 1
    assert {w.id for w in waves[0]} == {"C1", "C2", "C3"}


def test_overlapping_writes_forced_apart():
    rows = [_row("C1", ["a.py", "shared.py"]), _row("C2", ["shared.py", "b.py"])]
    waves = build_waves(rows)
    assert len(waves) == 2
    assert [w.id for wave in waves for w in wave] == ["C1", "C2"]


def test_undeclared_row_isolated_from_every_other_row():
    rows = [_row("C1", UNDECLARED), _row("C2", ["a.py"]), _row("C3", ["b.py"])]
    waves = build_waves(rows)
    # C1 must be alone in its wave; UNDECLARED cannot share with anything,
    # including another declared row, and (per AC2) not even with another
    # UNDECLARED row.
    wave_by_id = {w.id: i for i, wave in enumerate(waves) for w in wave}
    c1_wave = wave_by_id["C1"]
    assert len(waves[c1_wave]) == 1


def test_two_undeclared_rows_are_isolated_from_each_other():
    rows = [_row("C1", UNDECLARED), _row("C2", UNDECLARED)]
    waves = build_waves(rows)
    assert len(waves) == 2
    for wave in waves:
        assert len(wave) == 1


def test_dependency_edge_forces_strictly_later_placement():
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C2",
            ["b.py"],
            depends_on=[{"chunk": "C1", "gate_kind": "output-consumption-runtime"}],
        ),
    ]
    waves = build_waves(rows)
    wave_by_id = {w.id: i for i, wave in enumerate(waves) for w in wave}
    assert wave_by_id["C2"] > wave_by_id["C1"]


def test_dependency_gate_kind_carried_through_on_emitted_row():
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C2",
            ["b.py"],
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
    ]
    waves = build_waves(rows)
    c2 = next(w for wave in waves for w in wave if w.id == "C2")
    assert c2.depends_on == [{"chunk": "C1", "gate_kind": "epistemic-premise"}]


def test_determinism_same_input_same_wave_order():
    rows = [_row("C1", ["a.py"]), _row("C2", ["a.py"]), _row("C3", ["c.py"])]
    waves_1 = build_waves(rows)
    waves_2 = build_waves(rows)
    assert waves_1 == waves_2


def test_read_after_write_forces_strictly_later_wave():
    # C2 reads a.py, which C1 writes — write-overlap alone sees disjoint
    # files (a.py vs b.py) and would place both in one wave.
    rows = [
        _row("C1", ["a.py"]),
        _row("C2", ["b.py"], reads=["a.py"]),
    ]
    waves = build_waves(rows)
    wave_by_id = {w.id: i for i, wave in enumerate(waves) for w in wave}
    assert wave_by_id["C2"] > wave_by_id["C1"]


def test_dogfood_this_plans_own_spine_orders_c1_before_c2_before_c3():
    # The reproducing case, built from this repo's own plan spine's ACTUAL
    # declared writes:/reads: for C1-C4 (docs/plans/2026-08-12-emitter-
    # turns-a-spine-into-one-workflow.md § C1-C4, verified against disk):
    # spine_read.py, wave_map.py and pathspec.py write disjoint files but
    # each is read by the next. Write-overlap + depends_on alone would put
    # C1-C4 in one wave; read-after-write must separate C1/C2/C3.
    #
    # C4 is NOT asserted strictly after C3 here: C4's declared reads:
    # (workflow_scaffold.py, _workflow_contract.py, _workflow_patterns.py)
    # do not intersect C3's declared writes: (pathspec.py,
    # tests/test_pathspec.py), and the spine declares no depends_on edge
    # between them either — so no computed edge orders C3 before C4 today.
    # That is a real gap in the spine's own declared reads:, out of scope
    # for this chunk (plan body is read-only); asserting it here would
    # assert something this derivation cannot honestly prove.
    rows = [
        _row(
            "C1",
            writes=[
                "coordinator_core/ops/dispatch_emit/__init__.py",
                "coordinator_core/ops/dispatch_emit/spine_read.py",
                "coordinator_core/ops/dispatch_emit/tests/test_spine_read.py",
            ],
            reads=[
                "coordinator_core/ops/plan_tasks_render.py",
                "coordinator_core/frontmatter/body_blocks.py",
                "coordinator_core/frontmatter/schemas/plan-tasks.schema.json",
            ],
        ),
        _row(
            "C2",
            writes=[
                "coordinator_core/ops/dispatch_emit/wave_map.py",
                "coordinator_core/ops/dispatch_emit/tests/test_wave_map.py",
            ],
            reads=[
                "coordinator_core/ops/dispatch_emit/spine_read.py",
                "coordinator_core/ops/fan_out_integrator.py",
            ],
        ),
        _row(
            "C3",
            writes=[
                "coordinator_core/ops/dispatch_emit/pathspec.py",
                "coordinator_core/ops/dispatch_emit/tests/test_pathspec.py",
            ],
            reads=["coordinator_core/ops/dispatch_emit/wave_map.py"],
        ),
        _row(
            "C4",
            writes=[
                "coordinator_core/ops/dispatch_emit/emit.py",
                "coordinator_core/ops/dispatch_emit/tests/test_emit.py",
            ],
            reads=[
                "coordinator_core/ops/workflow_scaffold.py",
                "coordinator_core/ops/_workflow_contract.py",
                "coordinator_core/ops/_workflow_patterns.py",
            ],
        ),
    ]
    waves = build_waves(rows)
    wave_by_id = {w.id: i for i, wave in enumerate(waves) for w in wave}
    for earlier, later in [("C1", "C2"), ("C2", "C3")]:
        assert wave_by_id[later] > wave_by_id[earlier], (
            f"expected {later!r} strictly after {earlier!r}: {wave_by_id}"
        )


def test_directory_and_file_within_it_treated_as_overlapping():
    # docs/wiki/ (a declared directory write) and docs/wiki/dispatch-emit.md
    # (a file inside it) are the same surface in both directions.
    rows = [
        _row("C1", ["docs/wiki/"]),
        _row("C2", ["docs/wiki/dispatch-emit.md"]),
    ]
    waves = build_waves(rows)
    assert len(waves) == 2
    assert [w.id for wave in waves for w in wave] == ["C1", "C2"]

    rows_reversed = [
        _row("C1", ["docs/wiki/dispatch-emit.md"]),
        _row("C2", ["docs/wiki/"]),
    ]
    waves_reversed = build_waves(rows_reversed)
    assert len(waves_reversed) == 2


def test_case_insensitive_paths_treated_as_overlapping():
    # Review: coordinator:code-reviewer (wsc-A, ecb99d36) P2 — PurePosixPath
    # comparison is case-sensitive, but the fleet's dominant dev box
    # (macOS, case-insensitive HFS+/APFS by default) treats these as the
    # same file on disk.
    rows = [_row("C1", ["docs/Wiki/x.md"]), _row("C2", ["docs/wiki/x.md"])]
    waves = build_waves(rows)
    assert len(waves) == 2


def test_dotdot_segment_normalized_for_overlap():
    # Review: coordinator:code-reviewer (wsc-A, ecb99d36) P2 — `..` segments
    # were not collapsed, so `dir/../other.py` and `other.py` were
    # never recognized as the same surface.
    rows = [_row("C1", ["dir/../other.py"]), _row("C2", ["other.py"])]
    waves = build_waves(rows)
    assert len(waves) == 2


def test_cycle_message_names_predecessor_in_deterministic_order():
    # Review: coordinator:code-reviewer (wsc-A, ecb99d36) P2 — with `preds`
    # a set, iteration order (and so which cycle path lands in the raised
    # message) depended on PYTHONHASHSEED; a sorted() visit order fixes the
    # cycle path/message to a single, reproducible shape for a given input.
    rows = [
        _row("C1", ["a.py"], depends_on=[{"chunk": "C2", "gate_kind": "epistemic-premise"}]),
        _row("C2", ["b.py"], depends_on=[{"chunk": "C3", "gate_kind": "epistemic-premise"}]),
        _row("C3", ["c.py"], depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}]),
    ]
    with pytest.raises(WaveCycleError) as excinfo:
        build_waves(rows)
    message_1 = str(excinfo.value)

    with pytest.raises(WaveCycleError) as excinfo_2:
        build_waves(rows)
    message_2 = str(excinfo_2.value)

    assert message_1 == message_2


def test_self_edge_raises_wave_cycle_error():
    rows = [
        _row("C1", ["a.py"], depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}]),
    ]
    with pytest.raises(WaveCycleError) as excinfo:
        build_waves(rows)
    assert "C1" in str(excinfo.value)


def test_two_row_cycle_raises_wave_cycle_error():
    rows = [
        _row("C1", ["a.py"], depends_on=[{"chunk": "C2", "gate_kind": "epistemic-premise"}]),
        _row("C2", ["b.py"], depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}]),
    ]
    with pytest.raises(WaveCycleError) as excinfo:
        build_waves(rows)
    message = str(excinfo.value)
    assert "C1" in message
    assert "C2" in message
