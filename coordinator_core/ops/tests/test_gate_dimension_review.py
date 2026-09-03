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

import subprocess
from pathlib import Path
from typing import List

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


_HDR = gate_dimension_review._SHA_HEADER_PREFIX


def _log_z(*records: "tuple[str, str]") -> "tuple[int, str, str]":
    """`git log --pretty=format:{prefix}%H --name-only -z` output:
    NUL-separated records, each a `\\x01`-prefixed sha newline-joined to a
    path it touched."""
    return 0, "".join(f"{_HDR}{sha}\n{path}\0" for sha, path in records), ""


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


def test_review_calls_read_reviewed_set_once_with_no_legacy_fan_out(
    monkeypatch,
) -> None:
    """Pins this module's own call shape only, not `read_reviewed_set`'s
    internal subprocess cost (that's `review_trail.reviewed_set`'s own
    contract, out of this slice): `_review_dimension_check` calls
    `read_reviewed_set` exactly once, and neither retiring symbol
    (`list_paths`/`build_reviewed_set`, the pre-C3 git fan-out) survives
    the C3 re-pointing (docs/plans/2026-08-27-the-reviewed-set-is-a-file-
    not-a-computation.md § C3)."""
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
    `<review-trail-root>/findings/*.md` sidecars and never touch the
    creditable `*.json` (or `archive/review-trail/*.json`) records this
    dimension's reviewed set is built from. This test pins that scope
    against the reap core's own live path source (`scan_findings`'s
    `findings/` join under `machinery_paths.review_trail_dir`), not a
    retired literal constant (Review: overengineering-reviewer -- the prior
    `_FINDINGS_SUBPATH` pin went dead in production when `scan_findings` was
    rewritten to derive its root from `review_trail_dir`; this pin tracks
    that live source directly so a future widening still fails loudly)."""
    import inspect

    import coordinator_core.ops.fleet._findings_reap as findings_reap
    from coordinator_core.session.machinery_paths import review_trail_dir

    import os

    assert review_trail_dir("repo") == os.path.join(
        "repo", ".coordinator-local", "review-trail"
    )
    # scan_findings must join "findings" onto review_trail_dir()'s result --
    # never point directly at the review-trail root (the *.json record
    # corpus) or any other path. Pinned against the function's own source
    # rather than a standalone constant, so a future rewrite that drops the
    # "findings" join fails this test instead of silently widening scope.
    source = inspect.getsource(findings_reap.scan_findings)
    assert 'review_trail_dir(str(worktree_root))) / "findings"' in source, (
        "scan_findings must scope its walk to the findings/ subdir under "
        "the live review-trail root -- widening to the review-trail root "
        "itself could delete or rewrite a *.json record this dimension's "
        "reviewed-set store credits permanently"
    )


def test_review_drops_blank_changed_files_entries_no_dot_slash_normalization(
    monkeypatch,
) -> None:
    """The reverse direction of the windows-spelling test above: exercises
    `wanted`'s own construction rather than the git-side lines. Blank/
    whitespace-only entries are silently dropped by the `if p.strip()`
    guard; a `./`-prefixed entry is a distinct literal from its bare form
    (no directory normalization on the changed_files side), so only the
    exact spelling git prints can match it."""
    monkeypatch.setattr(
        gate_dimension_review, "_run_git", lambda args, cwd: _log_z((_SHA_A, "foo.py"))
    )
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )
    result = gate_dimension_review._review_dimension_check(
        ["", "   ", "./foo.py", "foo.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.FAIL, (
        "the exact 'foo.py' entry matches and is unreviewed; blanks and the "
        "'./foo.py' literal must not crash or spuriously widen the match"
    )


def test_review_handles_commit_with_empty_diff(monkeypatch) -> None:
    """`git commit --allow-empty` (or any commit touching nothing) prints a
    sha header with zero following path lines — the parser must not choke
    on it, and it must never enter commit_shas since no `elif` branch
    fires for it."""
    out = f"{_HDR}{_SHA_A}\0{_HDR}{_SHA_B}\nfoo.py\0"
    monkeypatch.setattr(
        gate_dimension_review, "_run_git", lambda args, cwd: (0, out, "")
    )
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )
    result = gate_dimension_review._review_dimension_check(
        ["foo.py"], "abc..HEAD", "/repo"
    )
    assert result.verdict is Verdict.FAIL
    assert "1/1" in result.detail, (
        "only _SHA_B (the one with a matching path line) is uncovered; the "
        "empty-diff _SHA_A must never enter commit_shas at all"
    )


def _git(args: "List[str]", cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
        # ONE creationflags source, not two. `_CREATIONFLAGS` IS
        # `no_console_creationflags()` (gate_dimension_review.py:122), so
        # spreading both passed the same keyword twice. On POSIX both spread
        # to `{}` and the duplicate is invisible; on Windows they carry
        # `creationflags` and every test using this helper died with
        # `TypeError: subprocess.run() got multiple values for keyword
        # argument 'creationflags'` -- a Windows-only red in a repo where
        # Windows is first-class.
        **gate_dimension_review._CREATIONFLAGS,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _commit_file(repo: Path, rel_path: str, message: str) -> str:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(message, encoding="utf-8")
    _git(["add", rel_path], repo)
    _git(["commit", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def test_review_against_real_git_covers_merge_and_hex_named_path(tmp_path) -> None:
    """No fixture in this file exercises real git's own `-z` /
    `--pretty=format:` / `--name-only` record framing — every other test
    shares the `_log_z` fixture's assumption about that shape with the
    implementation, which is exactly how both P1s this test pins survived.
    Runs a real throwaway repo covering, in one pass: a normal commit, a
    merge commit (must be credited via --diff-merges=first-parent), and a
    changed file whose basename is exactly 40 hex characters (must not be
    misread as a commit header)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    normal_sha = _commit_file(repo, "normal_file.py", "c1")

    _git(["checkout", "-b", "feature"], repo)
    feature_sha = _commit_file(repo, "feature.py", "on feature")
    _git(["checkout", "main"], repo)
    _commit_file(repo, "main_side.py", "on main")
    _git(["merge", "--no-ff", "-m", "merge feature", "feature"], repo)
    merge_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    hex_name = "a1b2c3d4e5" * 4
    assert len(hex_name) == 40
    (repo / hex_name).write_text("hex-named content", encoding="utf-8")
    (repo / "other.py").write_text("other", encoding="utf-8")
    _git(["add", hex_name, "other.py"], repo)
    _git(["commit", "-m", "hex-named path commit"], repo)
    hex_commit_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    changed_files = ["normal_file.py", "feature.py", hex_name, "other.py"]
    # normal_sha itself is the range's lower bound and excluded by `git log
    # <base>..HEAD`'s own exclusivity. `feature_sha` reaches the log both
    # directly (plain `git log` traverses all ancestors, not just
    # first-parent) and via merge_sha's own first-parent diff crediting
    # feature.py -- either way it, merge_sha, and hex_commit_sha are the
    # three in-range commits touching changed_files.
    all_covered = {feature_sha, merge_sha, hex_commit_sha}

    orig_read_reviewed_set = gate_dimension_review.read_reviewed_set
    gate_dimension_review.read_reviewed_set = lambda repo_root: all_covered
    try:
        covered_result = gate_dimension_review._review_dimension_check(
            changed_files, f"{normal_sha}..HEAD", repo,
        )
    finally:
        gate_dimension_review.read_reviewed_set = orig_read_reviewed_set

    assert covered_result.verdict is Verdict.PASS, covered_result.detail

    gate_dimension_review.read_reviewed_set = lambda repo_root: {
        feature_sha,
        hex_commit_sha,
    }
    try:
        uncovered_result = gate_dimension_review._review_dimension_check(
            changed_files, f"{normal_sha}..HEAD", repo
        )
    finally:
        gate_dimension_review.read_reviewed_set = orig_read_reviewed_set

    assert uncovered_result.verdict is Verdict.FAIL
    assert "1/3" in uncovered_result.detail, (
        "merge_sha alone dropped from the reviewed set must leave exactly "
        "1 of the 3 recognised commits uncovered -- proving the merge "
        "commit was itself credited via --diff-merges=first-parent, not "
        "merely riding along on feature_sha's plain traversal"
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


# ---------------------------------------------------------------------------
# Second credit source: the reviewer sidecar receipt.
#
# These tests exist because the FIRST source went stale silently. The
# reviewed-set store is fed only by `state/review-trail/*.json` folded at
# write time, and that corpus froze when `review_trail.write` lost its last
# production call site (DR-372, DR-374). Measured in this clone 2026-08-28:
# the store's newest covered commit sat 486 commits behind HEAD and none of
# the last 400 commits were members, so this dimension returned FAIL for
# every recent chain whether or not review had happened.
#
# The failure being repaired is a STUCK NEGATIVE, which is why both
# directions are pinned below. A suite asserting only that unreviewed work
# still FAILs would pass identically against a credit source that reads
# nothing at all -- i.e. against the bug.
# ---------------------------------------------------------------------------

_SESSION = "11112222-3333-4444-5555-666677778888"
_FSEP = gate_dimension_review._HEADER_FIELD_SEP


def _log_z_full(*records: "tuple[str, str, str, str]") -> "tuple[int, str, str]":
    """`git log` output with the FULL three-field commit header this module
    now requests: sha, committer date, and the `Session-Id` trailer, joined
    by `\x1f` on the one `\x01`-prefixed line, paths on the lines after."""
    return (
        0,
        "".join(
            f"{_HDR}{sha}{_FSEP}{when}{_FSEP}{sid}\n{path}\0"
            for sha, when, sid, path in records
        ),
        "",
    )


def test_receipt_credits_a_commit_the_stale_store_missed(monkeypatch) -> None:
    """THE POSITIVE, and the one that fails if the repoint reads nothing.
    Empty store (the real, frozen state) + a session receipt stamped after
    the commit -> PASS."""
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _log_z_full(
            (_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION, "a.py")
        ),
    )
    monkeypatch.setattr(gate_dimension_review, "read_reviewed_set", lambda repo_root: set())
    monkeypatch.setattr(
        gate_dimension_review, "receipt_credited_shas", lambda root, rows: {_SHA_A}
    )
    result = gate_dimension_review._review_dimension_check(["a.py"], "abc..HEAD", "/repo")
    assert result.verdict is Verdict.PASS
    assert "sidecar receipt" in result.detail


def test_still_fails_when_neither_source_credits(monkeypatch) -> None:
    """THE NEGATIVE. The gate must keep its ability to say no, or the repoint
    has replaced a useless FAIL with a useless PASS."""
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _log_z_full(
            (_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION, "a.py")
        ),
    )
    monkeypatch.setattr(gate_dimension_review, "read_reviewed_set", lambda repo_root: set())
    monkeypatch.setattr(
        gate_dimension_review, "receipt_credited_shas", lambda root, rows: set()
    )
    result = gate_dimension_review._review_dimension_check(["a.py"], "abc..HEAD", "/repo")
    assert result.verdict is Verdict.FAIL
    assert "1/1" in result.detail


def test_partial_receipt_credit_still_fails_on_the_remainder(monkeypatch) -> None:
    """Crediting some commits must not credit the rest -- the arithmetic in
    the detail line is the thing an operator acts on."""
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _log_z_full(
            (_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION, "a.py"),
            (_SHA_B, "2026-08-28T12:00:00+00:00", _SESSION, "a.py"),
        ),
    )
    monkeypatch.setattr(gate_dimension_review, "read_reviewed_set", lambda repo_root: set())
    monkeypatch.setattr(
        gate_dimension_review, "receipt_credited_shas", lambda root, rows: {_SHA_A}
    )
    result = gate_dimension_review._review_dimension_check(["a.py"], "abc..HEAD", "/repo")
    assert result.verdict is Verdict.FAIL
    assert "1/2" in result.detail


def test_receipt_source_is_not_consulted_when_the_store_covers_everything(
    monkeypatch,
) -> None:
    """The store is the cheap path (one `os.stat`). A fully-covered range must
    not pay for the sidecar walk or `parse_frontmatter`'s 29ms import."""
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _log_z_full(
            (_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION, "a.py")
        ),
    )
    monkeypatch.setattr(gate_dimension_review, "read_reviewed_set", lambda repo_root: {_SHA_A})

    def explode(root, rows):
        raise AssertionError("receipt credit consulted on a fully-covered range")

    monkeypatch.setattr(gate_dimension_review, "receipt_credited_shas", explode)
    result = gate_dimension_review._review_dimension_check(["a.py"], "abc..HEAD", "/repo")
    assert result.verdict is Verdict.PASS


def test_receipt_source_receives_date_and_session_from_the_same_git_log(
    monkeypatch,
) -> None:
    """The whole cost claim rests on this: no second spawn. The date and
    trailer must arrive parsed out of the header the dimension already
    requested, and ONLY for the commits the store left uncovered."""
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _log_z_full(
            (_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION, "a.py"),
            (_SHA_B, "2026-08-28T12:00:00+00:00", "", "a.py"),
        ),
    )
    monkeypatch.setattr(gate_dimension_review, "read_reviewed_set", lambda repo_root: {_SHA_B})

    seen: "list[tuple[str, str, str]]" = []

    def capture(root, rows):
        seen.extend(rows)
        return set()

    monkeypatch.setattr(gate_dimension_review, "receipt_credited_shas", capture)
    gate_dimension_review._review_dimension_check(["a.py"], "abc..HEAD", "/repo")
    assert seen == [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]


def test_trailerless_header_still_parses_as_a_commit(monkeypatch) -> None:
    """A commit with no `Session-Id` trailer yields an empty third field. It
    must still be recognised as a commit and still be REQUIRED to carry
    review evidence -- a missing trailer is not a free pass."""
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _log_z_full((_SHA_A, "2026-08-28T11:00:00+00:00", "", "a.py")),
    )
    monkeypatch.setattr(gate_dimension_review, "read_reviewed_set", lambda repo_root: set())
    result = gate_dimension_review._review_dimension_check(["a.py"], "abc..HEAD", "/repo")
    assert result.verdict is Verdict.FAIL
    assert "1/1" in result.detail


def test_git_log_format_requests_date_and_session_trailer() -> None:
    """Pins the format string itself. `separator=%x20` is load-bearing:
    without it git terminates each trailer with a newline, the value lands on
    its own line, and the path loop reads it as a touched path."""
    fmt = gate_dimension_review._COMMIT_HEADER_FORMAT
    assert fmt.startswith(gate_dimension_review._SHA_HEADER_PREFIX + "%H")
    assert "%cI" in fmt
    assert "key=Session-Id" in fmt
    assert "separator=%x20" in fmt
