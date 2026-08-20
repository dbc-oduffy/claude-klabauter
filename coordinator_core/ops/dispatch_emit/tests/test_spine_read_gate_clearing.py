"""Tests for spine_read._has_uncleared_execution_gate's clearing rule.

docs/plans/2026-08-20-gate-readers-stop-self-clearing.md AC1/AC2: a gate is
cleared ONLY by an explicit ``cleared: true``; ``closure_evidence`` is
purely descriptive and never clears anything on its own, however truthy.
Table-driven over the four shapes named in that plan's § Problem, plus the
``blocks: ac-closure`` and fail-closed-``blocks`` cases already asserted
elsewhere in ``test_spine_read.py`` and re-asserted here against the new
clearing rule specifically.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import read_spine

_HEADER = "# fixture plan\n\n## Tasks\n\n"


def _write_plan(tmp_path, body: str):
    path = tmp_path / "plan.md"
    path.write_text(_HEADER + "```yaml plan-tasks\n" + body + "\n```\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("case_id", "gate_entry_yaml", "expect_dispatchable"),
    [
        (
            "closure_evidence-truthy-no-cleared-key",
            """      condition: their thing must ship first
      blocks: execution
      closure_evidence: >-
        Not yet received.
""",
            # This is the defect row (AC1): presence of closure_evidence
            # alone used to clear the gate (ADMITTED). It must now withhold.
            False,
        ),
        (
            "closure_evidence-truthy-cleared-false",
            """      condition: their thing must ship first
      blocks: execution
      cleared: false
      closure_evidence: >-
        Not yet received.
""",
            # Already withheld before this change; must stay withheld.
            False,
        ),
        (
            "no-closure-evidence-at-all",
            """      condition: their thing must ship first
      blocks: execution
""",
            # Already withheld before this change; must stay withheld.
            False,
        ),
        (
            "cleared-true-no-evidence-named",
            """      condition: their thing must ship first
      blocks: execution
      cleared: true
""",
            # Already the clearing path before this change, and after this
            # change it is the ONLY clearing path.
            True,
        ),
    ],
)
def test_four_clearing_shapes(tmp_path, case_id, gate_entry_yaml, expect_dispatchable):
    body = (
        "- id: C1\n"
        f"  title: {case_id}\n"
        "  surface: some/surface\n"
        "  external_gate:\n"
        "    - owner_repo: some-other-repo\n"
        f"{gate_entry_yaml}"
    )
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == ({"C1"} if expect_dispatchable else set())


def test_blocks_ac_closure_never_withholds_regardless_of_cleared(tmp_path):
    # blocks: ac-closure holds only a named acceptance criterion open, not
    # the row's execution -- this must stay true under the new rule exactly
    # as it did under the old one, with no cleared: true present at all.
    body = """\
- id: C1
  title: only an acceptance criterion is gated
  surface: some/surface
  external_gate:
    - owner_repo: some-other-repo
      condition: their thing must ship before the AC can close
      blocks: ac-closure
"""
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C1"}


@pytest.mark.parametrize("blocks_value", [None, "typo-value"])
def test_absent_or_unrecognized_blocks_resolves_to_execution_and_withholds(
    tmp_path, blocks_value
):
    # Fail closed: only the literal "ac-closure" spares a row. An absent or
    # unrecognized `blocks` resolves to execution, so a typo cannot silently
    # disarm the gate -- asserted here against the new clearing rule too.
    blocks_line = "" if blocks_value is None else f"      blocks: {blocks_value}\n"
    body = (
        "- id: C1\n"
        "  title: fail-closed blocks, no cleared-true key\n"
        "  surface: some/surface\n"
        "  external_gate:\n"
        "    - owner_repo: some-other-repo\n"
        "      condition: their thing must ship first\n"
        f"{blocks_line}"
    )
    plan_path = _write_plan(tmp_path, body)
    ids = {row.id for row in read_spine(plan_path)}

    assert ids == set()
