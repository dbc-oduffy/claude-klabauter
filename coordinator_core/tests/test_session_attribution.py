"""
coordinator_core.test_session_attribution

Tests for the consolidated SHA -> Session-Id attribution classifier in
coordinator_core.session_attribution, exercised directly against real git
fixtures (temp repos with real commits carrying real Session-Id trailers) —
matching the idiom coordinator_core/ops/ceremony/tests/test_wsc_resolve.py
and coordinator_core/coverage.py's own module already use for this exact
classification logic, rather than mocking git.

Placed under coordinator_core/tests/, matching the actually-dominant
convention for this repo (coordinator_core/tests/test_*.py outnumbers flat
coordinator_core/test_*.py siblings, and single top-level modules of
comparable size/shape — git_ancestry.py, liveness.py — both have their
tests in coordinator_core/tests/, not flat beside the module). Originally
placed flat on a since-corrected claim that flat was the dominant local
convention for this repo; moved here on review.
# Review: code-reviewer — flat placement rested on a "dominant convention"
# claim the tree contradicts (tests/ subdir outnumbers flat siblings, and
# the two most comparable modules both use tests/); moved, not just
# re-justified.

Coverage:
  - the two-signal classifier against known-scope-path fixtures: an
    own-trailered commit, a foreign-trailered commit, an in-scope
    trailerless commit, and the NON-VACUITY anchor (an out-of-scope
    trailerless commit must be flagged foreign, never silently attributed);
  - contiguity: an in-scope set contiguous with the writing session's own
    trailer-attributed commits vs. one interleaved with a foreign commit;
  - the trailer-only fast path (`trailer_foreign_shas`) producing identical
    results to coverage.py's thin wrapper (`_narrow_foreign_session_scope`)
    for the scope="session" case;
  - each caller's PRESERVED, DELIBERATELY DIVERGENT failure posture on a git
    failure: coverage.py's wrapper fails CLOSED (raises
    `_ForeignSessionLookupError`); the two-signal classifier
    (`detect_foreign_commits`, what wsc_resolve.py calls) fails EMPTY
    (returns `[]`, its documented "could not determine" result).

Spec backlink: docs/plans/2026-07-27-review-trail-scope-guard.md § C5
(C5a/C5b/C5c were a dispatch-time wave-map expansion, not plan-doc text —
the plan carries one C5 task-spine row).
# Review: code-reviewer — backlink cited a "§ C5b" heading the plan doc
# never wrote; corrected to the actual § C5 anchor.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core import coverage, session_attribution


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, check=True)


def _init_repo(root: Path) -> str:
    """Initialize a real git repo at root; return the init commit's SHA."""
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "test@test.com"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    return _commit(root, "init", files={".gitkeep": "init\n"})


def _commit(
    root: Path,
    message: str,
    *,
    files: Optional[dict] = None,
    date: Optional[str] = None,
) -> str:
    """Write `files` (rel_path -> content), commit them, return the new HEAD SHA."""
    if files:
        for rel_path, content in files.items():
            path = root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    _git(["add", "-A"], root)
    env = None
    if date is not None:
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(root), capture_output=True, check=True, env=env,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo_root(tmp_path) -> Path:
    return tmp_path / "repo"


# ---------------------------------------------------------------------------
# Two-signal classifier — detect_foreign_commits
# ---------------------------------------------------------------------------


def test_trailer_naming_writing_session_is_own_not_foreign(repo_root):
    sid = "sess-attr-own-001"
    init_sha = _init_repo(repo_root)
    own_sha = _commit(
        repo_root, f"own work\n\nSession-Id: {sid}",
        files={"own.txt": "own\n"},
    )

    foreign = session_attribution.detect_foreign_commits(
        repo_root, sid, f"{init_sha}..HEAD", frozenset(),
    )
    assert own_sha not in foreign


def test_trailer_naming_different_session_is_foreign(repo_root):
    sid = "sess-attr-foreign-001"
    other_sid = "sess-attr-foreign-OTHER"
    init_sha = _init_repo(repo_root)
    theirs_sha = _commit(
        repo_root, f"their work\n\nSession-Id: {other_sid}",
        files={"theirs.txt": "theirs\n"},
    )

    foreign = session_attribution.detect_foreign_commits(
        repo_root, sid, f"{init_sha}..HEAD", frozenset(),
    )
    assert theirs_sha in foreign


def test_trailerless_commit_inside_known_scope_is_in_scope(repo_root):
    sid = "sess-attr-inscope-001"
    init_sha = _init_repo(repo_root)
    scoped_sha = _commit(
        repo_root, "trailerless work, in known scope",
        files={"scoped/file.txt": "scoped\n"},
    )

    foreign = session_attribution.detect_foreign_commits(
        repo_root, sid, f"{init_sha}..HEAD", frozenset({"scoped/file.txt"}),
    )
    assert scoped_sha not in foreign


def test_trailerless_commit_outside_known_scope_is_not_silently_safe(repo_root):
    """Non-vacuity anchor: a trailerless commit whose touched paths fall
    OUTSIDE the known-scope set must be classified foreign — asserting the
    negative directly, so a regression that silently treats "unknown" as
    "in-scope" is caught rather than passing by omission."""
    sid = "sess-attr-outscope-001"
    init_sha = _init_repo(repo_root)
    unscoped_sha = _commit(
        repo_root, "trailerless work, outside known scope",
        files={"unscoped/file.txt": "unscoped\n"},
    )

    foreign = session_attribution.detect_foreign_commits(
        repo_root, sid, f"{init_sha}..HEAD", frozenset({"scoped/file.txt"}),
    )
    assert unscoped_sha in foreign, (
        "a trailerless commit outside known_scope_paths must be classified "
        "foreign, never silently treated as in-scope/safe"
    )


# ---------------------------------------------------------------------------
# Contiguity — range_is_contiguous_suffix
# ---------------------------------------------------------------------------


def test_inscope_set_contiguous_with_own_trailer_commits(repo_root):
    sid = "sess-attr-contig-001"
    init_sha = _init_repo(repo_root)
    _commit(
        repo_root, f"own work 1\n\nSession-Id: {sid}",
        files={"a.txt": "a\n"}, date="2020-06-01T00:00:00Z",
    )
    _commit(
        repo_root, f"own work 2\n\nSession-Id: {sid}",
        files={"b.txt": "b\n"}, date="2020-06-02T00:00:00Z",
    )
    _commit(
        repo_root, "trailerless in-scope tail",
        files={"scoped/c.txt": "c\n"}, date="2020-06-03T00:00:00Z",
    )

    candidate_range = f"{init_sha}..HEAD"
    foreign = session_attribution.detect_foreign_commits(
        repo_root, sid, candidate_range, frozenset({"scoped/c.txt"}),
    )
    assert foreign == []
    assert session_attribution.range_is_contiguous_suffix(
        repo_root, candidate_range, foreign,
    ) is True


def test_inscope_set_interleaved_with_foreign_commit_is_not_contiguous(repo_root):
    sid = "sess-attr-contig-002"
    other_sid = "sess-attr-contig-002-OTHER"
    init_sha = _init_repo(repo_root)
    _commit(
        repo_root, f"own work 1\n\nSession-Id: {sid}",
        files={"a.txt": "a\n"}, date="2020-06-01T00:00:00Z",
    )
    foreign_sha = _commit(
        repo_root, f"foreign interloper\n\nSession-Id: {other_sid}",
        files={"foreign.txt": "foreign\n"}, date="2020-06-02T00:00:00Z",
    )
    _commit(
        repo_root, "trailerless in-scope tail",
        files={"scoped/c.txt": "c\n"}, date="2020-06-03T00:00:00Z",
    )

    candidate_range = f"{init_sha}..HEAD"
    foreign = session_attribution.detect_foreign_commits(
        repo_root, sid, candidate_range, frozenset({"scoped/c.txt"}),
    )
    assert foreign == [foreign_sha]
    assert session_attribution.range_is_contiguous_suffix(
        repo_root, candidate_range, foreign,
    ) is False


# ---------------------------------------------------------------------------
# Trailer-only fast path — equivalence with coverage.py's pre-refactor caller
# ---------------------------------------------------------------------------


def test_trailer_fast_path_matches_coverage_wrapper_for_session_scope(repo_root):
    sid = "sess-attr-fastpath-001"
    other_sid = "sess-attr-fastpath-001-OTHER"
    init_sha = _init_repo(repo_root)
    _commit(
        repo_root, f"own work\n\nSession-Id: {sid}",
        files={"own.txt": "own\n"},
    )
    theirs_sha = _commit(
        repo_root, f"their work\n\nSession-Id: {other_sid}",
        files={"theirs.txt": "theirs\n"},
    )

    sha_range = f"{init_sha}..HEAD"
    direct = session_attribution.trailer_foreign_shas(
        sha_range, sid, str(repo_root), {}, run=coverage._run,
    )
    via_wrapper = coverage._narrow_foreign_session_scope(
        sha_range, sid, str(repo_root), {},
    )

    assert direct == via_wrapper == frozenset({theirs_sha})


# ---------------------------------------------------------------------------
# Preserved, deliberately divergent failure postures
# ---------------------------------------------------------------------------


def test_coverage_caller_fails_closed_on_git_failure(repo_root):
    """coverage.py's wrapper raises _ForeignSessionLookupError (fail-CLOSED)
    on a backing git log failure — never a silent empty result."""
    sid = "sess-attr-failclosed-001"
    _init_repo(repo_root)
    bad_range = "not-a-real-revision..HEAD"

    with pytest.raises(coverage._ForeignSessionLookupError):
        coverage._narrow_foreign_session_scope(bad_range, sid, str(repo_root), {})


def test_wsc_resolve_caller_fails_empty_on_git_failure(repo_root):
    """The two-signal classifier (what wsc_resolve.py calls) returns [] on a
    backing git log failure (fail-EMPTY, its documented "could not
    determine" result) — a DIFFERENT posture from coverage.py's fail-closed
    wrapper, by design; asserting both callers behave the same way would
    assert the opposite of the spec."""
    sid = "sess-attr-failempty-001"
    _init_repo(repo_root)
    bad_range = "not-a-real-revision..HEAD"

    result = session_attribution.detect_foreign_commits(
        repo_root, sid, bad_range, frozenset(),
    )
    assert result == []
