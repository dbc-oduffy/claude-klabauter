"""
Pins bug 2026-09-23-a-stop-rule-halt-stops-the-whole-lane: a STOP-RULE-FIRED
halt composed for a single-plan spine still stops the whole run (byte-for-
byte, unchanged), while a mise-inventory compose spanning more than one
plan (rows carrying `Spec: <path> (<id>)` in their body -- see
`emit._row_source_plan`) scopes the halt to the stopping row's OWN plan and
lets every other plan's rows keep running.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _row(id_, writes, body="", surface="dispatch_emit"):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface=surface,
        writes=writes,
        reads=[],
        depends_on=[],
        body=body,
    )


def _spec_body(row_id: str, plan_path: str) -> str:
    # The exact shape `inventory_mint._row_body` writes -- the only place a
    # mise-inventory row's source plan lives.
    return f"Spec: {plan_path} ({row_id})\nSummary: does the thing.\n"


# ---------------------------------------------------------------------------
# Single-plan compose: whole-run halt, unchanged.
# ---------------------------------------------------------------------------


def test_single_plan_compose_keeps_whole_run_halt():
    waves = [
        [_row("C1", ["a.py"])],
        [_row("C2", ["b.py"])],
    ]
    script = compose_script(
        waves,
        name="single-plan",
        description="single-plan spine",
        plan_path="docs/plans/example.md",
    )

    assert "_haltedPlans" not in script
    assert "_skipIfHalted" not in script
    assert "_rowPlan" not in script
    # The pre-existing whole-run halt gate: fires straight off the batch's
    # own `_stopped...Results` array, no plan-scoping in between.
    assert "if (_stoppedWave1Results.length) return { halted:" in script
    assert "if (_stoppedWave2Results.length) return { halted:" in script


def test_single_plan_compose_unaffected_by_a_wave_row_with_no_spec_line():
    # A row's `body` carrying ordinary prose (no `Spec:` line) must not be
    # mistaken for a per-row plan -- `_row_source_plan` returns None for it,
    # same as the empty-body case above.
    waves = [[_row("C1", ["a.py"], body="Just do the thing.\n")]]
    script = compose_script(
        waves,
        name="single-plan-prose-body",
        description="single-plan spine with a non-Spec body",
        plan_path="docs/plans/example.md",
    )
    assert "_haltedPlans" not in script
    assert "if (_stoppedWave1Results.length) return { halted:" in script


# ---------------------------------------------------------------------------
# Multi-plan compose (mise inventory): plan-scoped skip, run continues.
# ---------------------------------------------------------------------------


def test_multi_plan_compose_scopes_halt_to_the_stopping_rows_plan():
    waves = [
        [
            _row("P1-C1", ["a.py"], body=_spec_body("P1-C1", "docs/plans/p1.md")),
            _row("P2-C1", ["b.py"], body=_spec_body("P2-C1", "docs/plans/p2.md")),
        ],
        [
            _row("P1-C2", ["c.py"], body=_spec_body("P1-C2", "docs/plans/p1.md")),
            _row("P2-C2", ["d.py"], body=_spec_body("P2-C2", "docs/plans/p2.md")),
        ],
    ]
    script = compose_script(
        waves,
        name="multi-plan",
        description="mise-inventory spine spanning two plans",
        plan_path="state/mise-inventory/example.spine.md",
    )

    # Runtime scaffolding is declared once, script-wide.
    assert "const _rowPlan = {" in script
    assert "'P1-C1': 'docs/plans/p1.md'" in script
    assert "'P2-C1': 'docs/plans/p2.md'" in script
    assert "const _haltedPlans = new Set();" in script
    assert "function _skipIfHalted(id, fn)" in script

    # Every dispatched row is wrapped in the skip-check, not called bare.
    assert "_skipIfHalted('P1-C1', () => agent(" in script
    assert "_skipIfHalted('P1-C2', () => agent(" in script
    assert "_skipIfHalted('P2-C1', () => agent(" in script
    assert "_skipIfHalted('P2-C2', () => agent(" in script

    # No whole-run halt tied to a STOP RULE anywhere -- the stop-rule
    # bookkeeping only ever records into _haltedPlans/_haltedPlanReasons.
    # (The preflight/commit gates keep their OWN unrelated `{ halted: ... }`
    # returns -- this only pins the stop-rule gate specifically.)
    assert "STOP RULE in the chunk's own spec fired" not in script
    assert "if (_stoppedWave1Results.length) return { halted:" not in script
    assert "if (_stoppedWave2Results.length) return { halted:" not in script
    assert "for (const id of _stoppedWave1Results)" in script
    assert "_haltedPlans.add(p);" in script
    assert "_haltedPlanReasons.set(p, id);" in script


def test_multi_plan_requires_at_least_two_distinct_plans():
    # Every row citing the SAME plan is not a multi-plan compose -- stays on
    # the whole-run halt, same as a single-plan spine with no Spec: line at
    # all.
    waves = [
        [_row("C1", ["a.py"], body=_spec_body("C1", "docs/plans/p1.md"))],
        [_row("C2", ["b.py"], body=_spec_body("C2", "docs/plans/p1.md"))],
    ]
    script = compose_script(
        waves,
        name="one-plan-with-spec-lines",
        description="mise-inventory spine over a single plan",
        plan_path="state/mise-inventory/example.spine.md",
    )
    assert "_haltedPlans" not in script
    assert "if (_stoppedWave1Results.length) return { halted:" in script
