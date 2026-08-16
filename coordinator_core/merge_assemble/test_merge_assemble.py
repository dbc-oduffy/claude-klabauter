"""Tests for the C2 fix: `portability_disposition`'s circular depends_on
edge over d5 (portability-sweep). Also covers the C3 fix: `apply()`
actually populating the `gates` key its module docstring already claimed
it filled in.

Spec backlink: pln-the-engine-asks-for-facts-it-a-8709a3, chunk C2, C3
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.merge_assemble import (
    GATE_DIRECTIVE_IDS,
    _assert_resolves_depends_on_invariant,
    build_directives,
    build_gate_verdicts_scaffold,
    build_judgment_points,
)
from coordinator_core.merge_assemble.apply import _fill_gate_verdicts


def test_d5_dispatches_without_a_supplied_portability_disposition() -> None:
    directives = build_directives(Path("."), tag_prefix="v", proposed_tag="v1.2.3")
    d5 = next(d for d in directives if d["id"] == "d5")
    assert d5["depends_on"] is None


def test_portability_disposition_evidence_names_real_result_not_absence() -> None:
    points_without_result = build_judgment_points()
    without = next(p for p in points_without_result if p["id"] == "portability_disposition")
    assert "has not run yet" not in without["evidence"]

    points_with_result = build_judgment_points(
        portability_sweep_result="no non-portable path literals found"
    )
    with_result = next(
        p for p in points_with_result if p["id"] == "portability_disposition"
    )
    assert "no non-portable path literals found" in with_result["evidence"]

    proceed = next(d for d in with_result["dispositions"] if d["value"] == "proceed")
    assert "d5" not in proceed["resolves"]


def test_resolves_depends_on_invariant_passes_for_current_shape() -> None:
    directives = build_directives(Path("."), tag_prefix="v", proposed_tag="v1.2.3")
    judgment_points = build_judgment_points()
    _assert_resolves_depends_on_invariant(directives, judgment_points)


def test_resolves_depends_on_invariant_catches_dangling_edge() -> None:
    directives = [
        {"id": "dX", "depends_on": None},
    ]
    judgment_points = [
        {
            "id": "some_point",
            "dispositions": [{"value": "proceed", "resolves": ["dX"]}],
        }
    ]
    # Review: code-reviewer — pytest.raises over bare try/except for
    # idiomatic style and a clearer failure message on regression.
    with pytest.raises(RuntimeError):
        _assert_resolves_depends_on_invariant(directives, judgment_points)


def test_active_branch_guard_absent_from_scaffold_and_has_no_directive() -> None:
    scaffold = build_gate_verdicts_scaffold()
    assert "active_branch_guard" not in scaffold

    directives = build_directives(Path("."), tag_prefix="v", proposed_tag="v1.2.3")
    directive_ids = {d["id"] for d in directives}
    assert "active_branch_guard" not in directive_ids


def test_fill_gate_verdicts_populates_gates_key_from_a_run() -> None:
    report = {
        "landed": ["d0", "d1", "d2", "d4", "d5", "d6"],
        "results": [
            {"id": "d5", "already_satisfied": False, "detail": {"cli": "portability-sweep"}},
            {"id": "d6", "already_satisfied": False, "detail": {"cli": "check-no-illegal-paths"}},
        ],
    }
    gates = _fill_gate_verdicts(report)
    assert set(GATE_DIRECTIVE_IDS) <= set(gates)
    assert gates["portability_sweep"] == "passed"
    assert gates["check_no_illegal_paths"] == "passed"


def test_fill_gate_verdicts_marks_failed_directive_failed_and_rest_pending() -> None:
    report = {
        "error": "portability-sweep: exited 1: some failure",
        "failed_directive": "d5",
        "landed": ["d0", "d1", "d2", "d4"],
        "results": [],
    }
    gates = _fill_gate_verdicts(report)
    assert gates["portability_sweep"] == "failed"
    assert gates["check_no_illegal_paths"] == "pending"
