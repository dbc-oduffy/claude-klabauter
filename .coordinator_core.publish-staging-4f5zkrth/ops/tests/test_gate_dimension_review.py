"""
coordinator_core.ops.tests.test_gate_dimension_review

Unit tests for the "review" dimension (C5,
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C5) — pinning the
verdict mapping (covered/uncovered/UNAVAILABLE -> PASS/FAIL/UNAVAILABLE),
the diff_base/repo_root UNAVAILABLE guards, and the self-registration seam
plug-in against `gate_validate_invocable`'s own registry.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C5
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

import coordinator_core.ops.gate_dimension_review as gate_dimension_review
import coordinator_core.ops.gate_validate_invocable as gate_validate_invocable
from coordinator_core.ops.gate_validate_invocable import (
    DIMENSION_NAMES,
    Verdict,
    _DIMENSION_REGISTRY,
)
from coordinator_core.ops.list_review_trail_records import ReviewTrailListError


def test_review_registered_in_seam() -> None:
    assert "review" in DIMENSION_NAMES
    assert _DIMENSION_REGISTRY["review"] is gate_dimension_review._review_dimension_check


def test_review_unavailable_without_diff_base() -> None:
    result = gate_dimension_review._review_dimension_check(["a.py"], None, "/repo")
    assert result.verdict is Verdict.UNAVAILABLE
    assert "diff_base" in result.detail


def test_review_unavailable_without_repo_root() -> None:
    result = gate_dimension_review._review_dimension_check(["a.py"], "abc..HEAD", None)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "repo_root" in result.detail


def test_review_covered_when_no_changed_files() -> None:
    result = gate_dimension_review._review_dimension_check([], "abc..HEAD", "/repo")
    assert result.verdict is Verdict.PASS
    assert "empty" in result.detail


def test_review_covered_when_no_touching_commits(monkeypatch) -> None:
    monkeypatch.setattr(
        gate_dimension_review, "_run_git", lambda args, cwd: (0, "", "")
    )
    result = gate_dimension_review._review_dimension_check(
        ["a.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.PASS
    assert "no commits" in result.detail


def test_review_unavailable_on_git_log_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: (1, "", "fatal: bad revision"),
    )
    result = gate_dimension_review._review_dimension_check(
        ["a.py"], "bogus..HEAD", "/repo"
    )
    assert result.verdict is Verdict.UNAVAILABLE
    assert "bad revision" in result.detail


def test_review_unavailable_when_trail_corpus_unreadable(monkeypatch) -> None:
    monkeypatch.setattr(
        gate_dimension_review, "_run_git", lambda args, cwd: (0, "deadbeef", "")
    )

    def _raise() -> None:
        raise ReviewTrailListError("cannot resolve state/review-trail/")

    monkeypatch.setattr(gate_dimension_review, "list_paths", lambda: _raise())
    result = gate_dimension_review._review_dimension_check(
        ["a.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.UNAVAILABLE
    assert "review-trail corpus" in result.detail


def test_review_covered_when_all_commits_reviewed(monkeypatch) -> None:
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: (0, "deadbeef\ncafef00d", ""),
    )
    monkeypatch.setattr(gate_dimension_review, "list_paths", lambda: ["trail.json"])
    monkeypatch.setattr(
        gate_dimension_review,
        "build_reviewed_set",
        lambda trail_paths, on_record_error, intersect_shas, repo_root: {
            "deadbeef",
            "cafef00d",
        },
    )
    result = gate_dimension_review._review_dimension_check(
        ["a.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.PASS
    assert "covered" in result.detail


def test_review_uncovered_when_a_commit_is_unreviewed(monkeypatch) -> None:
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: (0, "deadbeef\ncafef00d", ""),
    )
    monkeypatch.setattr(gate_dimension_review, "list_paths", lambda: ["trail.json"])
    monkeypatch.setattr(
        gate_dimension_review,
        "build_reviewed_set",
        lambda trail_paths, on_record_error, intersect_shas, repo_root: {"deadbeef"},
    )
    result = gate_dimension_review._review_dimension_check(
        ["a.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.FAIL
    assert "uncovered" in result.detail


def test_review_result_dimension_name_matches_seam_contract() -> None:
    result = gate_dimension_review._review_dimension_check([], "abc..HEAD", "/repo")
    assert result.dimension == "review"


def test_seam_run_dimension_uses_registered_review_check(monkeypatch) -> None:
    """End-to-end through gate_validate_invocable's own dispatch, proving the
    self-registration actually plugs into the seam's _run_dimension path."""
    monkeypatch.setattr(
        gate_dimension_review, "_run_git", lambda args, cwd: (0, "", "")
    )
    result = gate_validate_invocable._run_dimension(
        "review", ["a.py"], "abc..HEAD", "/repo"
    )
    assert result.dimension == "review"
    assert result.verdict is Verdict.PASS
