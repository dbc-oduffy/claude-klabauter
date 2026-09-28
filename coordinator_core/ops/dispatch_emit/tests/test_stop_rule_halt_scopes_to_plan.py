"""
Pins bug 2026-09-23-a-stop-rule-halt-stops-the-whole-lane, carried into the
DAG rewrite (§ Design D4): a STOP-RULE-FIRED halt on a row with no source
plan sets the GLOBAL ``_halted`` flag (single-plan compose, whole run
stops); a mise-inventory compose spanning more than one plan (rows
carrying ``Spec: <path> (<id>)`` in their body -- see
``emit._row_source_plan``) scopes the halt to the stopping row's OWN plan
via ``_haltedPlans``/``_haltedPlanReasons``, letting every other plan's
rows keep running. ``_rowPlan``/``_haltedPlans``/``_haltedPlanReasons`` are
now ALWAYS declared (empty for the single-plan case) -- one ``_runRow``
code path, no more ``_skipIfHalted``/``if _multi_plan:`` branching.
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
    return f"Spec: {plan_path} ({row_id})\nSummary: does the thing.\n"


def test_single_plan_compose_declares_empty_plan_tables_and_the_global_halt():
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

    assert "const _rowPlan = {  };" in script
    assert "const _haltedPlans = new Set();" in script
    assert "const _haltedPlanReasons = new Map();" in script
    assert "let _halted = null;" in script
    assert "_halted = id;" in script


def test_single_plan_compose_unaffected_by_a_wave_row_with_no_spec_line():
    waves = [[_row("C1", ["a.py"], body="Just do the thing.\n")]]
    script = compose_script(
        waves,
        name="single-plan-prose-body",
        description="single-plan spine with a non-Spec body",
        plan_path="docs/plans/example.md",
    )
    assert "const _rowPlan = {  };" in script


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

    assert "const _rowPlan = {" in script
    assert "'P1-C1': 'docs/plans/p1.md'" in script
    assert "'P2-C1': 'docs/plans/p2.md'" in script
    assert "const _haltedPlans = new Set();" in script
    assert "_haltedPlans.add(plan);" in script
    assert "_haltedPlanReasons.set(plan, id);" in script
    assert "_skipIfHalted" not in script

    assert "_rows['P1-C1'] = _runRow('P1-C1'," in script
    assert "_rows['P2-C1'] = _runRow('P2-C1'," in script


def test_multi_plan_requires_at_least_two_distinct_plans():
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
    assert "'C1': 'docs/plans/p1.md'" in script
    assert "'C2': 'docs/plans/p1.md'" in script
