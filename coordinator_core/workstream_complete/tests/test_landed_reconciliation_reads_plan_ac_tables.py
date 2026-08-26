"""Guard: the landed-reconciliation gate must read the AC grammar plans
actually use.

WHAT THIS PREVENTS. `compute_landed_reconciliation_gate` counted `- [ ]`
checkboxes only. Handoffs spell acceptance criteria that way; PLANS spell them
as `| ACn | criterion | status |` table rows -- 226 of the recent plans under
`docs/plans/` against 21 with checkboxes, measured 2026-08-26. So on ~91% of
the corpus it guards, the gate returned `indeterminate` ("is status: landed but
its Acceptance Criteria heading has no checkboxes") no matter what the criteria
said. It did that against a plan whose 16 criteria were every one of them met,
and because that indeterminate feeds `jp-landed-reconciliation-block-stamp`, it
blocked the `implemented` stamp on a fully-discharged plan.

A gate that abstains on the dominant format is not being conservative. It is
failing to look, in the one case it exists for, while reporting a token that
reads like caution.

Negative-spec:
    - Does NOT widen `parse_consumed_handoff_acceptance_criteria`. That parser
      is also leg A of `consumed_handoff_completeness`, where the checkbox
      contract is right; teaching it a second grammar would change handoff
      semantics to fix a plan-reading bug. The table reader is a sibling, and
      the gate tries checkboxes first and falls back.
    - Does NOT assert that an unrecognised status token is treated as done. It
      is treated as OPEN, on purpose, and this file pins that direction: a
      false "done" lets a landed plan with open criteria stamp terminal, which
      is the failure the gate exists to prevent; a false "open" costs one WARN.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.workstream_complete import compute_landed_reconciliation_gate
from coordinator_core.workstream_complete.directives_session_hygiene import (
    parse_consumed_handoff_acceptance_criteria,
    parse_plan_acceptance_criteria_table,
)

_TABLE_PLAN = """---
status: landed
---

## Acceptance Criteria

| ID | Criterion | Status |
|---|---|---|
| AC1 | first | **met** — landed at `abc1234` |
| AC2 | second | **met** — landed at `def5678` |

## Tasks
"""

_TABLE_PLAN_WITH_AN_OPEN_ROW = _TABLE_PLAN.replace(
    "| AC2 | second | **met** — landed at `def5678` |",
    "| AC2 | second | open |",
)


def test_table_rows_are_counted_where_checkboxes_find_nothing() -> None:
    assert parse_consumed_handoff_acceptance_criteria(_TABLE_PLAN)["total"] == 0
    assert parse_plan_acceptance_criteria_table(_TABLE_PLAN) == {"done": 2, "total": 2, "open": 0}


def test_open_and_partial_rows_count_as_open() -> None:
    assert parse_plan_acceptance_criteria_table(_TABLE_PLAN_WITH_AN_OPEN_ROW)["open"] == 1

    partial = _TABLE_PLAN.replace("| AC2 | second | **met** — landed at `def5678` |",
                                  "| AC2 | second | **partial** — half of it |")
    assert parse_plan_acceptance_criteria_table(partial)["open"] == 1


def test_an_unrecognised_status_counts_as_open_not_done() -> None:
    """The safe direction. A false 'done' stamps a terminal state over open
    criteria; a false 'open' costs one WARN."""
    odd = _TABLE_PLAN.replace("| AC2 | second | **met** — landed at `def5678` |",
                              "| AC2 | second | probably fine? |")
    assert parse_plan_acceptance_criteria_table(odd)["open"] == 1


def test_no_acceptance_criteria_heading_still_returns_none() -> None:
    assert parse_plan_acceptance_criteria_table("---\nstatus: landed\n---\n\n## Tasks\n") is None


def test_suffixed_ac_ids_are_counted() -> None:
    """Real plans carry AC7b / AC9c alongside AC7 / AC9; a digits-only id
    pattern would silently undercount exactly the plans with the most
    criteria."""
    suffixed = _TABLE_PLAN.replace("| AC2 | second |", "| AC2b | second |")
    assert parse_plan_acceptance_criteria_table(suffixed)["total"] == 2


def test_gate_resolves_a_fully_met_table_plan_as_not_applicable(tmp_path: Path) -> None:
    """End to end: the regression that blocked the stamp. A landed plan with
    every table row met must not report `indeterminate`."""
    plan = tmp_path / "plan.md"
    plan.write_text(_TABLE_PLAN, encoding="utf-8")

    gate = compute_landed_reconciliation_gate("plan", plan)

    assert gate.verdict == "not-applicable", gate.summary_line
    assert gate.open_count == 0
    assert gate.total_count == 2


def test_gate_still_warns_on_a_landed_table_plan_with_an_open_row(tmp_path: Path) -> None:
    """The other half: reading the table must not turn the gate into a rubber
    stamp. A genuinely open row still fires."""
    plan = tmp_path / "plan.md"
    plan.write_text(_TABLE_PLAN_WITH_AN_OPEN_ROW, encoding="utf-8")

    gate = compute_landed_reconciliation_gate("plan", plan)

    assert gate.verdict == "applicable"
    assert gate.open_count == 1
    assert gate.total_count == 2


def test_gate_is_still_indeterminate_when_neither_grammar_is_present(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("---\nstatus: landed\n---\n\n## Acceptance Criteria\n\nprose only.\n\n## Tasks\n",
                    encoding="utf-8")

    gate = compute_landed_reconciliation_gate("plan", plan)

    assert gate.verdict == "indeterminate"
    assert "neither checkboxes nor" in gate.summary_line


def test_checkbox_plans_are_untouched_by_the_fallback(tmp_path: Path) -> None:
    """The 21 checkbox-spelled plans must keep their existing behaviour --
    the fallback fires only where the checkbox parse found nothing."""
    plan = tmp_path / "plan.md"
    plan.write_text("---\nstatus: landed\n---\n\n## Acceptance Criteria\n\n- [x] one\n- [ ] two\n",
                    encoding="utf-8")

    gate = compute_landed_reconciliation_gate("plan", plan)

    assert gate.verdict == "applicable"
    assert gate.open_count == 1
