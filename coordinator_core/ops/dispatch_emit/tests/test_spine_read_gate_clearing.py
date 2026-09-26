
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
            False,
        ),
        (
            "no-closure-evidence-at-all",
            """      condition: their thing must ship first
      blocks: execution
""",
            False,
        ),
        (
            "cleared-true-no-evidence-named",
            """      condition: their thing must ship first
      blocks: execution
      cleared: true
""",
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
