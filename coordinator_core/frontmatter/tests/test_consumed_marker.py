"""Tests for coordinator_core.frontmatter.consumed_marker.

Spec backlink: coordinator-claude coordinator/bin/lib/consumed-marker.js.
"""
from __future__ import annotations

from coordinator_core.frontmatter.consumed_marker import (
    CONSUMED_MARKER_RE,
    TERMINAL_DEPLOYMENT,
    TERMINAL_STATUS,
)


# DR-084 dual-read, intentionally permanent: these constants re-export the
# widened lifecycle_constants SSOT, so they admit old ∪ new vocabulary and now
# diverge DELIBERATELY from the JS original (coordinator-claude lib/consumed-marker.js),
# which stays old-vocabulary-only until the fleet cutover. The divergence is
# the point -- claude-klabauter must treat a `claimed` / `continued` / `closed` record
# as terminal today. Narrowing is gated on the consumer-corpus exit condition
# in lifecycle_constants.py's module docstring (9d00b459 incident of record),
# not on a claude-klabauter-scoped signal.


def test_terminal_status_values():
    assert TERMINAL_STATUS == {"consumed", "superseded", "claimed"}


def test_terminal_deployment_values():
    assert TERMINAL_DEPLOYMENT == {"shipped", "abandoned", "continued", "closed"}


def test_marker_basic_match():
    m = CONSUMED_MARKER_RE.search("body <!-- consumed: 2026-05-14 --> tail")
    assert m is not None
    assert m.group(1) == "2026-05-14"
    # Python's `re` engine returns '' here (vs JS `undefined`) when the
    # optional non-capturing group doesn't participate -- both are falsy,
    # and callers treat this group's absence via truthiness, so this is a
    # benign engine-level divergence, not a behavior difference.
    assert not m.group(2)


def test_marker_with_notes():
    m = CONSUMED_MARKER_RE.search("<!-- consumed: 2026-05-14 shipped in PR 123 -->")
    assert m is not None
    assert m.group(1) == "2026-05-14"
    assert m.group(2) == "shipped in PR 123"


def test_marker_notes_with_gt_char_not_swallowed():
    # the Staff Engineer F4: lazy `(.*?)\s*-->` so `>` in notes is captured, not treated
    # as an early terminator by a greedy `[^>]*` stop.
    m = CONSUMED_MARKER_RE.search(
        "<!-- consumed: 2026-05-14 shipped via PR > main -->"
    )
    assert m is not None
    assert m.group(2) == "shipped via PR > main"


def test_marker_case_insensitive():
    m = CONSUMED_MARKER_RE.search("<!-- CONSUMED: 2026-05-14 -->")
    assert m is not None
    assert m.group(1) == "2026-05-14"


def test_marker_no_match():
    assert CONSUMED_MARKER_RE.search("no marker here") is None


def test_marker_bad_date_no_match():
    assert CONSUMED_MARKER_RE.search("<!-- consumed: not-a-date -->") is None
