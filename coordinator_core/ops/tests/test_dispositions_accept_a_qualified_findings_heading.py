"""The premise check's sidecar is a dispositionable reviewer sidecar.

`_FINDINGS_HEADING` was pinned to the exact line `## Findings`, so the
premise-check pass's `## Findings table (plan order)` matched neither supported
shape and was refused. That pass is a first-class producer in plan-blitz: the
wave translates its rows onto REVIEW_SCHEMA and they reach the same integrator
every reviewer's findings do. Refusing its sidecar meant those findings were
applied to the plan and never stamped on the sidecar they came from — the loss
`A-SIDECAR-THE-DISPOSITION-OP-REFUSES-LOSES-ONLY-THE-RECORD` names, where the
deliverable is fine and only the record is gone.

Measured 2026-09-10, plan-blitz run 20260910T000000Z: two separate fires
reported the refusal independently, each classing it a tooling defect rather
than a plan defect.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import append_integrator_dispositions as m


def _doc(heading: str) -> str:
    return f"# Review\n\n{heading}\n\n| # | class |\n|---|---|\n| 1 | REFS |\n"


@pytest.mark.parametrize(
    "heading",
    ["## Findings", "## Findings table (plan order)", "## Findings (3 blocking)"],
)
def test_a_qualified_findings_heading_is_still_a_findings_section(heading):
    shape, is_empty = m._detect_findings_shape(_doc(heading))
    assert shape == m._SHAPE_REVIEW_FINDINGS
    assert is_empty is False


def test_the_qualifier_is_not_left_in_the_body():
    """The slice starts at the end of the heading LINE. A fixed-length offset
    would leave the qualifier in the body and shift every boundary search."""
    body = m._extract_findings_section(_doc("## Findings table (plan order)"))
    assert body is not None
    assert "table (plan order)" not in body
    assert body.lstrip().startswith("| # | class |")


def test_a_prose_mention_is_still_not_a_heading():
    """The whole point of line-anchoring survives the qualifier."""
    assert m._extract_findings_section("Explaining the `## Findings` heading.\n") is None


def test_a_different_word_is_not_a_findings_heading():
    """`\\b` after the word, so a longer word does not match."""
    assert m._extract_findings_section(_doc("## Findingsomething")) is None


@pytest.mark.parametrize(
    "boundary", ["## Integrator Dispositions", "## Exit interview"]
)
def test_boundaries_stay_exact(boundary):
    """A section boundary that tolerates a suffix is a section that can end in
    the wrong place — `_find_heading`'s docstring records what each costs."""
    assert m._find_heading(f"{boundary} (partial)\n", boundary) is None
    assert m._find_heading(f"{boundary}\n", boundary) is not None
