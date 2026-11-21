"""leak_gate and attribution_gate at a many-path shape: work scales with the paths they judge."""

from __future__ import annotations


import pytest

from coordinator_core.ops.ceremony import commit_gates

pytestmark = [pytest.mark.complete]

N = 2000
N_PY = 2


def test_any_attribution_pattern_flags_are_ones_the_alternation_preserves():
    import re

    from coordinator_core.attribution.patterns import PATTERNS

    kept = re.IGNORECASE | re.UNICODE
    assert {n: p.flags & ~kept for n, p in PATTERNS.items() if p.flags & ~kept} == {}


def test_any_attribution_pattern_matches_a_line_each_pattern_matches():
    from coordinator_core.attribution.patterns import PATTERNS

    samples = {
        "review_colon": "# Review: the Staff Engineer (Finding 1)",
        "finding_ref": "see Finding 3",
        "per_review": "done per review",
        "staff_eng_finding": "staff-eng finding",
        "reviewer_finding": "reviewer finding",
        "review_corrected": "REVIEW-CORRECTED",
        "break_class_attributed": "BREAK-CLASS review",
    }
    for name, line in samples.items():
        assert PATTERNS[name].search(line), name
        assert commit_gates._ANY_ATTRIBUTION_PATTERN.search(line), name
