"""P7 negative spec: no warm measurement figure without a named clone.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
§ Hard constraints (P7), § C9.

WHAT THIS PINS. P7's ruling: "Any warm measurement must name its clone or it
is meaningless -- 63/63 warm on the mirror and 0/9 on the live tree were
both true." No `coordinator_core.warm` measurement-reporting helper exists
in this tree yet -- AC3/AC4/AC6b's own measurement work (C2 and later) is
what will emit real figures. This file pins the CONTRACT any such helper
must satisfy before that code lands, so a measurement helper cannot be
authored, land, and silently drop clone provenance with nothing to catch it
-- the same shape `require_named_clone` below enforces is importable by
that future code rather than re-derived.

NEGATIVE-SPEC:
    - Does NOT validate any REAL measurement artifact on disk (the
      docs/research/warm-engine-premise/*.md figures are prose, not a
      structured schema this test can parse without inventing one) -- this
      pins the reusable validator and proves it rejects the exact shape of
      violation P7 names, not a repo-wide scan.
    - Does NOT gate on figure VALUES (min-of-N, p50, etc, P8's job) -- only
      on the presence of a non-empty `clone` field alongside any reported
      figure.
"""

from __future__ import annotations

import pytest


def require_named_clone(report: dict) -> None:
    """Raise `ValueError` if `report` carries a figure with no named clone.

    A "figure" is any of the numeric measurement fields P8 names (`min_ms`,
    `p50_ms`, `p95_ms`, `max_ms`, `n`, `concurrent_sessions`) or a bare
    `value`. `clone` must be present and a non-empty string -- `None`, `""`,
    and a missing key are all violations, matching P7's "meaningless without
    one" framing rather than treating an empty string as a degraded-but-
    acceptable answer.
    """
    figure_keys = {"min_ms", "p50_ms", "p95_ms", "max_ms", "n", "concurrent_sessions", "value"}
    reports_a_figure = any(key in report for key in figure_keys)
    if not reports_a_figure:
        return
    clone = report.get("clone")
    if not isinstance(clone, str) or not clone.strip():
        raise ValueError(
            f"measurement report {report!r} reports a figure but names no clone -- "
            "P7: '63/63 warm on the mirror and 0/9 on the live tree were both true'. "
            "Every warm measurement must name the clone it was taken on."
        )


def test_a_report_with_no_clone_is_rejected():
    with pytest.raises(ValueError, match="names no clone"):
        require_named_clone({"p50_ms": 40.1, "n": 63})


def test_a_report_with_an_empty_clone_string_is_rejected():
    with pytest.raises(ValueError, match="names no clone"):
        require_named_clone({"p50_ms": 40.1, "clone": "  "})


def test_a_report_naming_its_clone_is_accepted():
    require_named_clone({"p50_ms": 40.1, "n": 63, "clone": "mirror-2026-08-19"})


def test_a_report_with_no_figures_at_all_needs_no_clone():
    """A pure metadata dict (no measurement figure present) is not what P7
    is about -- only a report that actually asserts a number is in scope."""
    require_named_clone({"note": "queue prototype, never reviewed"})
