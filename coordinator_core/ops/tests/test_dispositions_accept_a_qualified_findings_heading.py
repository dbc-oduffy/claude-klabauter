"""The premise check's sidecar is a verifiable reviewer sidecar.

`_FINDINGS_HEADING` detection must tolerate a qualifier after the word
(`## Findings (3 blocking)`, `## Findings table (plan order)`) — ported
from the retired `append_integrator_dispositions` module (DoE-claude
docs/plans/2026-09-26-retire-review-integrator.md, row M2): the premise-check
pass's own heading wording is `## Findings table (plan order)`, and a strict
match refused it, silently under-counting its declared findings against the
new `## Findings Ledger` block.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_findings_ledger as m


def _doc(heading: str) -> str:
    return f"# Review\n\n{heading}\n\n### Finding 1\nSomething.\n"


@pytest.mark.parametrize(
    "heading",
    ["## Findings", "## Findings table (plan order)", "## Findings (3 blocking)"],
)
def test_a_qualified_findings_heading_is_still_a_findings_section(heading):
    section = m._extract_findings_section(_doc(heading))
    assert section is not None
    assert "### Finding 1" in section


def test_the_qualifier_is_not_left_in_the_body():
    body = m._extract_findings_section(_doc("## Findings table (plan order)"))
    assert body is not None
    assert "table (plan order)" not in body


def test_a_prose_mention_is_still_not_a_heading():
    assert m._extract_findings_section("Explaining the `## Findings` heading.\n") is None


def test_a_different_word_is_not_a_findings_heading():
    assert m._extract_findings_section(_doc("## Findingsomething")) is None


@pytest.mark.parametrize("boundary", ["## Findings Ledger", "## Exit interview"])
def test_boundaries_stay_exact(boundary):
    assert m._find_heading(f"{boundary} (partial)\n", boundary) is None
    assert m._find_heading(f"{boundary}\n", boundary) is not None
