from __future__ import annotations

from coordinator_core.frontmatter.consumed_marker import (
    CONSUMED_MARKER_RE,
    TERMINAL_DEPLOYMENT,
    TERMINAL_STATUS,
)


# diverge DELIBERATELY from the JS original (DoE lib/consumed-marker.js),


def test_terminal_status_values():
    assert TERMINAL_STATUS == {"consumed", "superseded", "claimed"}


def test_terminal_deployment_values():
    assert TERMINAL_DEPLOYMENT == {"shipped", "abandoned", "continued", "closed"}


def test_marker_basic_match():
    m = CONSUMED_MARKER_RE.search("body <!-- consumed: 2026-05-14 --> tail")
    assert m is not None
    assert m.group(1) == "2026-05-14"
    assert not m.group(2)


def test_marker_with_notes():
    m = CONSUMED_MARKER_RE.search("<!-- consumed: 2026-05-14 shipped in PR 123 -->")
    assert m is not None
    assert m.group(1) == "2026-05-14"
    assert m.group(2) == "shipped in PR 123"


def test_marker_notes_with_gt_char_not_swallowed():
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
