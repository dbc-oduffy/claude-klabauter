"""
coordinator_core.ops.dispatch_emit.tests.test_unverifiable_enricher_row

Purpose: `emit._row_agent_type` refuses a row that derives to
coordinator:enricher while its own verification clause requires RUNNING
something. The enricher may not run tests; an executor may not write under
docs/plans/; so a close-out row writing its execution record beside its plan
and verifying with a test run cannot be run by any single agent, and without
this it halted a mise run mid-wave instead of failing at derivation
(example-retrieval-repo-4a, wf_8dc1b0ed-32b row D16, 2026-09-11).

Run (from repo root):
    python3 -m pytest coordinator_core/ops/dispatch_emit/tests/test_unverifiable_enricher_row.py -q
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import (
    UnverifiableEnricherRowError,
    _verification_requires_a_run,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, build_waves

_RECORD = "docs/plans/2026-09-10-silent-degradation-disclosure-half.execution-record.md"

#: D16's body, verbatim from the example-retrieval-repo spine that halted.
_D16_BODY = (
    "Spec: `docs/plans/2026-09-10-silent-degradation-disclosure-half.md`\n"
    "Verification (this row is DONE only when this holds): falsifier prints PASS "
    "against its own D2 baseline; targeted pytest set green\n"
    "Complexity: S\n"
)


def _row(writes, body="", agent_type=None):
    return WaveRow(
        id="D16",
        title="close-out",
        surface="dispatch_emit",
        writes=writes,
        reads=[],
        depends_on=[],
        agent_type=agent_type,
        body=body,
    )


def test_the_row_that_halted_is_refused_at_emit():
    with pytest.raises(UnverifiableEnricherRowError) as excinfo:
        compose_script([[_row([_RECORD], _D16_BODY)]], name="wf", description="d16")
    assert "Split it" in str(excinfo.value)


def test_a_plan_body_row_that_verifies_by_reading_still_goes_to_the_enricher():
    """The negative half. Most enricher rows verify by inspection, and this
    must not turn every docs/plans write into a refusal."""
    body = "Verification: the plan's § Revision history names the struck ACs\n"
    script = compose_script([[_row([_RECORD], body)]], name="wf", description="read")
    assert "agentType: 'coordinator:enricher'" in script


def test_a_row_with_no_verification_clause_is_not_refused():
    """Silence is not a statement — this refuses only what the row says."""
    script = compose_script(
        [[_row([_RECORD], "Spec: somewhere\npytest appears outside any clause\n")]],
        name="wf",
        description="no clause",
    )
    assert "agentType: 'coordinator:enricher'" in script


def test_an_executor_row_verifying_with_a_run_is_untouched():
    """Only the derived-enricher path is at issue; an executor may run tests."""
    script = compose_script(
        [[_row(["coordinator_core/ops/dispatch_emit/emit.py"], _D16_BODY)]],
        name="wf",
        description="executor",
    )
    assert "agentType: 'coordinator:executor'" in script


def test_an_explicit_agent_type_is_the_authors_escape():
    script = compose_script(
        [[_row([_RECORD], _D16_BODY, agent_type="coordinator:enricher")]],
        name="wf",
        description="override",
    )
    assert "agentType: 'coordinator:enricher'" in script


@pytest.mark.parametrize(
    "clause, runs",
    [
        ("Verification: targeted pytest set green", True),
        ("Verification: the falsifier prints PASS", True),
        ("Verification: the dispatch_emit test suite passes", True),
        ("Verification: the record names every AC and its disposition", False),
        ("Verification: a reader can find the gravestone from the plan", False),
        # The mise-prep lane, verbatim shape: validation the enricher is TOLD
        # to run, not a test it is forbidden to. Refusing it would break the
        # whole lane.
        (
            "Verification (this row is DONE only when this holds): Run `python3 "
            "coordinator/bin/mise-prep-gate.py --repo-root . docs/plans/x.md` until "
            "it prints PREPPED, then stamp with coordinator-invoke plan.stamp_prepped",
            False,
        ),
    ],
)
def test_the_run_signal_reads_the_clause(clause, runs):
    assert _verification_requires_a_run(clause) is runs


def test_the_body_reaches_emit_from_spine_text(tmp_path):
    """End-to-end. The body is carried through two tuples that each default
    it to empty, so a test constructing WaveRow directly cannot see a break in
    the parsing seam — which is how agent_type/agent_model first shipped
    inert (WaveRow's own docstring)."""
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "# fixture plan\n\n## Tasks\n\n"
        "```yaml plan-tasks\n"
        "- id: D16\n"
        "  title: close-out\n"
        "  surface: dispatch_emit\n"
        "  change_kind: doc-edit\n"
        "  body: |\n"
        "    Verification (this row is DONE only when this holds): targeted pytest set green\n"
        "  writes:\n"
        f"    - {_RECORD}\n"
        "```\n",
        encoding="utf-8",
    )

    with pytest.raises(UnverifiableEnricherRowError):
        compose_script(build_waves(read_spine(plan_path)), name="wf", description="e2e")
