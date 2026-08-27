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


# 40-hex or the parser treats it as a touched path, not a commit header. An
# abbreviated stub sha makes every test below pass vacuously via the
# "no commits touch changed_files" branch -- which is what happened when the
# pathspec moved out of argv and these stubs were not updated with it.
_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _log_z(*records: "tuple[str, str]") -> "tuple[int, str, str]":
    """`git log --pretty=format:%H --name-only -z` output: NUL-separated
    records, each a sha newline-joined to a path it touched."""
    return 0, "".join(f"{sha}\n{path}\0" for sha, path in records), ""


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
        lambda args, cwd: _log_z((_SHA_A, "a.py"), (_SHA_B, "a.py")),
    )
    monkeypatch.setattr(
        gate_dimension_review,
        "read_reviewed_set",
        lambda repo_root: {_SHA_A, _SHA_B},
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
        lambda args, cwd: _log_z((_SHA_A, "a.py"), (_SHA_B, "a.py")),
    )
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: {_SHA_A}
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
        gate_dimension_review, "_run_git", lambda args, cwd: _log_z((_SHA_A, "a.py"))
    )
    calls: list[str] = []

    def _fake_read_reviewed_set(repo_root: str):
        calls.append(repo_root)
        return {_SHA_A}

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


def test_review_cost_is_invariant_in_changed_files_above_argv_cap(monkeypatch) -> None:
    """Above the Windows argv cap the check must still answer, at ONE spawn.

    Two regressions are pinned here, in the order they were shipped.

    The original fail-open: `HEAD~200..HEAD` (1477 changed files) splatted
    `changed_files` into one argv, overflowed the Windows cap (WinError 206)
    and was swallowed into UNAVAILABLE -- a large changeset, the kind most
    worth reviewing, silently received no coverage.

    The fix for it: pathspec-batching that argv closed the fail-open but made
    the cost linear in the changeset -- 2000 paths measured 718.75ms across 55
    processes, over the DR-344 500ms brightline, on a branch whose bulk
    changesets run past 26,000 files. So the pathspec leaves argv entirely and
    the intersection happens in process.

    The invariant is therefore EXACTLY ONE `git log`, carrying no pathspec, at
    any changeset size.
    """
    changed_files = [f"some/long/enough/path/to/file_{i:05d}.py" for i in range(2000)]

    calls: list[list[str]] = []

    def _fake_run_git(args, cwd):
        calls.append(args)
        # `git log --name-only -z` shape: NUL-separated records, each a sha
        # newline-joined to the first of its touched paths. Two commits, one
        # touching a path the caller asked about and one touching a path it
        # did not -- so an implementation that credits every commit in the
        # range regardless of paths fails this test.
        return _log_z(
            (_SHA_A, "some/long/enough/path/to/file_00007.py"),
            (_SHA_B, "some/other/untouched/file.py"),
        )

    monkeypatch.setattr(gate_dimension_review, "_run_git", _fake_run_git)

    seen_reads: list[str] = []

    def _fake_read_reviewed_set(repo_root: str):
        seen_reads.append(repo_root)
        return {_SHA_A}

    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", _fake_read_reviewed_set
    )

    result = gate_dimension_review._review_dimension_check(
        changed_files, "abc..HEAD", "/repo"
    )

    assert len(calls) == 1, (
        "cost must be invariant in len(changed_files): exactly one git log, "
        f"got {len(calls)}"
    )
    assert "--" not in calls[0], "no pathspec may reach argv -- that is the cap overflow"
    for path in changed_files[:5]:
        assert path not in calls[0]
    assert len(" ".join(calls[0])) < 32767

    assert result.verdict is Verdict.PASS, (
        "the one commit touching a caller-named path is reviewed, so PASS -- "
        "never UNAVAILABLE, which is the fail-open this pins"
    )
    assert seen_reads == ["/repo"], "reviewed-set is read exactly once"


def test_review_ignores_commits_touching_no_caller_named_path(monkeypatch) -> None:
    """The in-process intersection must actually filter.

    Taking the pathspec out of argv means git no longer does the filtering,
    so a commit in the range that touches nothing the caller asked about must
    be dropped here -- otherwise every commit in the range gets credited and
    the gate reports on work it was never asked about.
    """
    def _fake_run_git(args, cwd):
        return _log_z((_SHA_A, "unrelated/module.py"), (_SHA_B, "also/unrelated.py"))

    monkeypatch.setattr(gate_dimension_review, "_run_git", _fake_run_git)
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )

    result = gate_dimension_review._review_dimension_check(
        ["the/only/path/i/asked/about.py"], "abc..HEAD", "/repo"
    )

    # No commit touches the caller's path, so nothing needs a review stamp --
    # PASS despite the reviewed-set being empty.
    assert result.verdict is Verdict.PASS
    assert "no commits" in result.detail


def test_review_matches_windows_spelled_changed_files(monkeypatch) -> None:
    """A caller passing backslashes must still match git's forward slashes.

    Windows is first-class here, and the intersection is now a string compare
    this module owns rather than a pathspec git normalises.
    """
    def _fake_run_git(args, cwd):
        return _log_z((_SHA_A, "coordinator_core/coverage.py"))

    monkeypatch.setattr(gate_dimension_review, "_run_git", _fake_run_git)
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )

    result = gate_dimension_review._review_dimension_check(
        [r"coordinator_core\coverage.py"], "abc..HEAD", "/repo"
    )

    assert result.verdict is Verdict.FAIL, (
        "the backslash-spelled path must match, leaving one unreviewed commit"
    )


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
