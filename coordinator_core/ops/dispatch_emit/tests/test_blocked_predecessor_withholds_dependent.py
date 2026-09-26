"""
IBMFR-R01: a depends_on gate onto a BLOCKED predecessor withholds the
dependent row at dispatch, together with its transitive dependents.

A predecessor is BLOCKED, from ``wave_map.build_waves``'s pure-function
vantage, exactly when it is absent from the rows passed in — the row simply
never appears in this test's ``rows`` list, standing in for any reason
``read_spine`` (or a caller) excluded it upstream. Without the fix, that
absence makes ``_predecessors`` silently drop the ordering edge (nothing to
order against) and the dependent schedules unblocked; with the fix, the
dependent (and anything that in turn depends on it) is held out of the wave
graph entirely.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import build_waves


def _row(row_id, writes, depends_on=(), reads=()):
    return EmitterRow(
        id=row_id,
        title=row_id,
        surface=row_id,
        writes=writes,
        reads=list(reads),
        depends_on=list(depends_on),
    )


def test_two_row_spine_with_blocked_c1_never_schedules_c2():
    """The row body's own test: a two-row spine with a BLOCKED C1 never
    schedules C2. C1 is BLOCKED by simply not appearing in ``rows`` at all;
    C2 gates ``output-consumption-runtime`` on it."""
    c2 = _row(
        "C2",
        writes=["c2.py"],
        depends_on=[{"chunk": "C1", "gate_kind": "output-consumption-runtime"}],
    )

    waves = build_waves([c2])

    scheduled_ids = {row.id for wave in waves for row in wave}
    assert "C2" not in scheduled_ids
    assert waves == []


def test_epistemic_premise_gate_onto_blocked_predecessor_also_withholds():
    """Same holdout for the other declarable runtime-order gate_kind."""
    c2 = _row(
        "C2",
        writes=["c2.py"],
        depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
    )

    waves = build_waves([c2])

    scheduled_ids = {row.id for wave in waves for row in wave}
    assert "C2" not in scheduled_ids


def test_holdout_is_transitive_over_a_blocked_grandparent():
    """A row depending on a row already held for a BLOCKED predecessor is
    itself withheld -- a row cannot run against a predecessor that did not
    run."""
    c2 = _row(
        "C2",
        writes=["c2.py"],
        depends_on=[{"chunk": "C1", "gate_kind": "output-consumption-runtime"}],
    )
    c3 = _row(
        "C3",
        writes=["c3.py"],
        depends_on=[{"chunk": "C2", "gate_kind": "output-consumption-runtime"}],
    )

    waves = build_waves([c2, c3])

    scheduled_ids = {row.id for wave in waves for row in wave}
    assert scheduled_ids == set()


def test_unspecified_gate_kind_onto_missing_predecessor_is_unaffected():
    """A depends_on edge with no gate_kind (or one outside the two runtime-
    order kinds) onto a missing chunk is not this holdout's concern -- it
    keeps its prior (unheld) behaviour: the dangling edge is simply dropped
    by ``_predecessors`` and the row schedules in wave 0."""
    c2 = _row(
        "C2",
        writes=["c2.py"],
        depends_on=[{"chunk": "C1"}],
    )

    waves = build_waves([c2])

    scheduled_ids = {row.id for wave in waves for row in wave}
    assert scheduled_ids == {"C2"}


def test_present_predecessor_with_runtime_gate_kind_still_orders_normally():
    """A sanity check that this holdout only fires when the predecessor is
    actually absent -- when C1 IS present (not BLOCKED), the ordinary
    depends_on ordering applies and both rows schedule, C1 first."""
    c1 = _row("C1", writes=["c1.py"])
    c2 = _row(
        "C2",
        writes=["c2.py"],
        depends_on=[{"chunk": "C1", "gate_kind": "output-consumption-runtime"}],
    )

    waves = build_waves([c1, c2])

    assert [row.id for wave in waves for row in wave] == ["C1", "C2"]
    assert len(waves) == 2


def test_undeclared_writes_epistemic_premise_holdout_still_works():
    """Regression: the pre-existing epistemic-premise + UNDECLARED-writes
    holdout (predecessor present, but this row's own writes unknown) is
    untouched by this change."""
    c1 = _row("C1", writes=["c1.py"])
    c2 = _row(
        "C2",
        writes=UNDECLARED,
        depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
    )

    waves = build_waves([c1, c2])

    scheduled_ids = {row.id for wave in waves for row in wave}
    assert scheduled_ids == {"C1"}
