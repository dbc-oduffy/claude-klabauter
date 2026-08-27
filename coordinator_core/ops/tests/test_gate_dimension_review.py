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


def test_review_covered_when_all_commits_reviewed(monkeypatch) -> None:
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: (0, "deadbeef\ncafef00d", ""),
    )
    monkeypatch.setattr(
        gate_dimension_review,
        "read_reviewed_set",
        lambda repo_root: {"deadbeef", "cafef00d"},
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
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: {"deadbeef"}
    )
    result = gate_dimension_review._review_dimension_check(
        ["a.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.FAIL
    assert "uncovered" in result.detail


def test_review_reads_reviewed_set_with_no_added_spawn(monkeypatch) -> None:
    """The reviewed-set membership check must be a pure read — no
    `list_paths`/`build_reviewed_set` git fan-out survives the C3
    re-pointing (docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-
    computation.md § C3)."""
    monkeypatch.setattr(
        gate_dimension_review, "_run_git", lambda args, cwd: (0, "deadbeef", "")
    )
    calls: list[str] = []

    def _fake_read_reviewed_set(repo_root: str):
        calls.append(repo_root)
        return {"deadbeef"}

    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", _fake_read_reviewed_set
    )
    result = gate_dimension_review._review_dimension_check(
        ["a.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.PASS
    assert calls == ["/repo"]
    assert not hasattr(gate_dimension_review, "build_reviewed_set")
    assert not hasattr(gate_dimension_review, "list_paths")


def test_review_result_dimension_name_matches_seam_contract() -> None:
    result = gate_dimension_review._review_dimension_check([], "abc..HEAD", "/repo")
    assert result.dimension == "review"


def test_review_batches_changed_files_above_argv_cap(monkeypatch) -> None:
    """Above the Windows argv cap, `changed_files` must be batched across
    multiple `git log` calls (never splatted unbounded into one argv) and
    the resulting commit lists unioned -- reproduces the fail-open trap at
    `HEAD~200..HEAD` (1477 changed files) where a single unbounded call
    overflowed argv (WinError 206) and was swallowed into UNAVAILABLE."""
    changed_files = [f"some/long/enough/path/to/file_{i:05d}.py" for i in range(2000)]

    calls: list[list[str]] = []

    def _fake_run_git(args, cwd):
        calls.append(args)
        # each chunk "discovers" one distinct commit, proving the union
        # actually accumulates across calls rather than only keeping the
        # last chunk's result.
        return 0, f"deadbeef{len(calls):04d}", ""

    monkeypatch.setattr(gate_dimension_review, "_run_git", _fake_run_git)

    seen_reads: list[str] = []

    def _fake_read_reviewed_set(repo_root: str):
        seen_reads.append(repo_root)
        # every synthesized commit is "reviewed" -- the point of this test
        # is the batching/union of commit_shas, not the reviewed-set content.
        return {f"deadbeef{i:04d}" for i in range(1, len(calls) + 1)}

    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", _fake_read_reviewed_set
    )

    result = gate_dimension_review._review_dimension_check(
        changed_files, "abc..HEAD", "/repo"
    )

    assert len(calls) > 1, "expected changed_files to be split across multiple git log calls"
    for args in calls:
        assert len(" ".join(args)) < 32767, "a single call must never exceed the argv cap"

    assert result.verdict is Verdict.PASS
    assert seen_reads == ["/repo"], "reviewed-set is read exactly once, never per chunk"


def test_reap_findings_scope_excludes_json_trail_records() -> None:
    """Monotonicity guard (module docstring's "Freshness rule, resident-set
    edition"): the reviewed-set store never observes a deletion, which is
    safe only because DR-218's reapers are scoped to
    `state/review-trail/findings/*.md` sidecars and never touch the
    creditable `state/review-trail/*.json` (or `archive/review-trail/
    *.json`) records this dimension's reviewed set is built from. This test
    pins that scope directly against the reaper modules' own constant, so a
    future widening of either reaper to the `*.json` corpus fails this test
    instead of silently defeating the append-only assumption above."""
    import coordinator_core.ops.fleet._findings_reap as findings_reap
    import coordinator_core.ops.fleet.reap_integrated_findings as reap_integrated
    import coordinator_core.ops.fleet.reap_unintegrated_findings as reap_unintegrated

    assert findings_reap._FINDINGS_SUBPATH == ("state", "review-trail", "findings")
    # Both reap legs (a: integrated, b: unintegrated) must scan through the
    # same shared, findings-scoped subpath -- neither leg may point directly
    # at "state/review-trail" (the *.json record corpus) or any other path.
    for mod in (reap_integrated, reap_unintegrated):
        subpath = getattr(mod, "_FINDINGS_SUBPATH", findings_reap._FINDINGS_SUBPATH)
        assert subpath == ("state", "review-trail", "findings")
        assert subpath[-1] == "findings", (
            "a reap leg scoped to the review-trail root (not the findings/ "
            "subdir) could delete or rewrite a *.json record this "
            "dimension's reviewed-set store credits permanently"
        )


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
