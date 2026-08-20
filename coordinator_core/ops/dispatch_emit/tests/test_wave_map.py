"""
Tests for coordinator_core.ops.dispatch_emit.wave_map.build_waves.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow
from coordinator_core.ops.dispatch_emit import wave_map
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


def test_epistemic_premise_undeclared_row_held_out_of_wave_graph(caplog):
    # The reproducing case (docs/plans/2026-08-19-the-fired-path-reaches-
    # the-engine.md, chunk C6): a row gated epistemic-premise on a
    # predecessor, with no writes: key at all, must not sink the emit for
    # the other, ready chunks -- it is held out instead, loudly.
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C6",
            UNDECLARED,
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
    ]
    with caplog.at_level("WARNING"):
        waves = build_waves(rows)

    all_ids = {w.id for wave in waves for w in wave}
    assert all_ids == {"C1"}
    assert any("C6" in record.message for record in caplog.records)


def test_epistemic_premise_gate_with_declared_writes_not_held():
    # Predicate half (b) fails: writes: is a real (even empty) list, not
    # UNDECLARED, so the row keeps current (unheld) behaviour exactly.
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C2",
            [],
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
    ]
    waves = build_waves(rows)
    all_ids = {w.id for wave in waves for w in wave}
    assert all_ids == {"C1", "C2"}


def test_undeclared_writes_without_epistemic_premise_gate_not_held():
    # Predicate half (a) fails: no epistemic-premise depends_on edge, so an
    # UNDECLARED row keeps its pre-existing isolated-wave behaviour (AC2)
    # rather than being held out.
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C2",
            UNDECLARED,
            depends_on=[{"chunk": "C1", "gate_kind": "output-consumption-runtime"}],
        ),
    ]
    waves = build_waves(rows)
    all_ids = {w.id for wave in waves for w in wave}
    assert all_ids == {"C1", "C2"}


def test_held_out_row_transitively_holds_its_own_dependent():
    # C7 depends on C6 (held). C7 cannot run against a predecessor that
    # did not run, so C7 must be held too, even though C7 itself declares
    # writes and has no epistemic-premise gate of its own.
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C6",
            UNDECLARED,
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
        _row(
            "C7",
            ["c7.py"],
            depends_on=[{"chunk": "C6", "gate_kind": "output-consumption-runtime"}],
        ),
    ]
    waves = build_waves(rows)
    all_ids = {w.id for wave in waves for w in wave}
    assert all_ids == {"C1"}


def test_cycle_among_held_out_rows_still_raises_wave_cycle_error():
    # MAJOR (review-c-pathspec-wavemap.md): a cycle consisting entirely of
    # epistemic-premise-gated UNDECLARED rows must not be silently swallowed
    # by holdout filtering. C1 gates epistemic-premise on C2 and C2 gates
    # epistemic-premise on C1; both independently satisfy _compute_held_out's
    # direct predicate (gated + UNDECLARED writes), so a holdout pass that
    # filters BEFORE cycle detection would strip both rows and never see the
    # mutual deadlock -- it would surface only as two indistinguishable
    # ordinary "waiting on an epistemic-premise gate" holds. Cycle detection
    # must run against the full, unfiltered predecessor graph.
    rows = [
        _row(
            "C1",
            UNDECLARED,
            depends_on=[{"chunk": "C2", "gate_kind": "epistemic-premise"}],
        ),
        _row(
            "C2",
            UNDECLARED,
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
    ]
    with pytest.raises(WaveCycleError) as excinfo:
        build_waves(rows)
    message = str(excinfo.value)
    assert "C1" in message
    assert "C2" in message


def test_held_out_row_transitively_holds_across_two_hops():
    # Two-hop extension of test_held_out_row_transitively_holds_its_own_
    # dependent: C8 depends on C7, which depends on held C6. This confirms
    # _compute_held_out's fixed-point loop actually iterates past one round
    # rather than only catching a single hop.
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C6",
            UNDECLARED,
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
        _row(
            "C7",
            ["c7.py"],
            depends_on=[{"chunk": "C6", "gate_kind": "output-consumption-runtime"}],
        ),
        _row(
            "C8",
            ["c8.py"],
            depends_on=[{"chunk": "C7", "gate_kind": "output-consumption-runtime"}],
        ),
    ]
    waves = build_waves(rows)
    all_ids = {w.id for wave in waves for w in wave}
    assert all_ids == {"C1"}


def test_epistemic_premise_constant_is_a_member_of_the_schema_enum():
    # MINOR (review-c-pathspec-wavemap.md): wave_map._EPISTEMIC_PREMISE and
    # schema_validate.py's hardcoded 'epistemic-premise' literal are two
    # independent copies of the same discriminator value, with nothing
    # asserting they agree. This does not unify them (a shared constant
    # would not catch enum drift originating on the schema side); it asserts
    # the wave_map half stays a member of plan-tasks.schema.json's
    # depends_on[].gate_kind enum, so drift there fails a test instead of
    # silently desyncing.
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "frontmatter"
        / "schemas"
        / "plan-tasks.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    gate_kind_enum = schema["properties"]["depends_on"]["items"]["properties"][
        "gate_kind"
    ]["enum"]
    assert wave_map._EPISTEMIC_PREMISE in gate_kind_enum


def test_held_out_row_never_stamps_disposition_or_mutates_input_rows():
    rows = [
        _row("C1", ["a.py"]),
        _row(
            "C6",
            UNDECLARED,
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
    ]
    snapshot = list(rows)
    build_waves(rows)
    assert rows == snapshot


def test_declared_edge_outranks_opposing_derived_read_after_write(caplog):
    # The reproducing case (example-retrieval-repo-em cross-repo memo, 2026-08-20):
    # C11 is a spike that reads subsystem.py AT HEAD and writes only a
    # verdict doc; C12 implements the verdict into subsystem.py and gates
    # epistemic-premise on C11. Unioning the declared edge with the derived
    # read-after-write edge refused a correctly-filled spine. The declared
    # edge wins; the derived one is dropped with a warning.
    rows = [
        _row("C11", ["docs/research/spike-verdicts/coupling.md"], reads=["tools/subsystem.py"]),
        _row(
            "C12",
            ["tools/subsystem.py", "tools/other.py"],
            depends_on=[{"chunk": "C11", "gate_kind": "epistemic-premise"}],
        ),
    ]
    with caplog.at_level("WARNING", logger=wave_map.__name__):
        waves = build_waves(rows)

    wave_by_id = {w.id: i for i, wave in enumerate(waves) for w in wave}
    assert wave_by_id["C11"] < wave_by_id["C12"], "declared order must survive"
    assert "dropped derived edge C11 -> C12" in caplog.text
    assert "tools/subsystem.py" in caplog.text


def test_derived_edge_survives_when_no_declared_edge_opposes_it():
    # The declared-beats-derived resolution must not disarm the derived rule
    # in the case it exists for: two writers, no declared edge either way.
    rows = [
        _row("C1", ["a.py"]),
        _row("C2", ["b.py"], reads=["a.py"]),
    ]
    waves = build_waves(rows)
    wave_by_id = {w.id: i for i, wave in enumerate(waves) for w in wave}
    assert wave_by_id["C1"] < wave_by_id["C2"]


def test_genuinely_circular_depends_on_still_refuses():
    # Declared-beats-derived resolves declared-vs-derived disagreement only.
    # A cycle made of declared edges alone survives it and still raises.
    rows = [
        _row("C1", ["a.py"], depends_on=[{"chunk": "C2", "gate_kind": "epistemic-premise"}]),
        _row("C2", ["b.py"], depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}]),
    ]
    with pytest.raises(WaveCycleError):
        build_waves(rows)


def test_cycle_message_names_each_legs_provenance():
    # A cycle of two derived edges: the message must say each leg was
    # derived and name the colliding path, not just the row ids.
    rows = [
        _row("C1", ["a.py"], reads=["b.py"]),
        _row("C2", ["b.py"], reads=["a.py"]),
    ]
    with pytest.raises(WaveCycleError) as excinfo:
        build_waves(rows)
    message = str(excinfo.value)
    assert "derived:" in message
    assert "a.py" in message and "b.py" in message


def test_cycle_message_distinguishes_declared_from_derived_legs():
    # Three rows: C1 -> C2 declared, C2 -> C3 declared, C3 -> C1 derived.
    # No pair carries opposing declared+derived edges, so nothing is
    # dropped and the cycle is real — the message must attribute each leg.
    rows = [
        _row("C1", ["a.py"], depends_on=[{"chunk": "C2", "gate_kind": "epistemic-premise"}]),
        _row("C2", ["b.py"], depends_on=[{"chunk": "C3", "gate_kind": "output-consumption-runtime"}]),
        _row("C3", ["c.py"], reads=["a.py"]),
    ]
    with pytest.raises(WaveCycleError) as excinfo:
        build_waves(rows)
    message = str(excinfo.value)
    assert "declared: depends_on, gate_kind=epistemic-premise" in message
    assert "declared: depends_on, gate_kind=output-consumption-runtime" in message
    assert "derived: C3 reads a.py, written by C1" in message


def test_self_edge_message_carries_provenance():
    rows = [
        _row("C1", ["a.py"], depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}]),
    ]
    with pytest.raises(WaveCycleError) as excinfo:
        build_waves(rows)
    message = str(excinfo.value)
    assert "C1" in message
    assert "declared: depends_on, gate_kind=epistemic-premise" in message
