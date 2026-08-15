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

Spec backlink: pln-review-trail-scope-guard-refus-d6e42c § C5
(C5a/C5b/C5c were a dispatch-time wave-map expansion, not plan-doc text —
the plan carries one C5 task-spine row).
# Review: code-reviewer — backlink cited a "§ C5b" heading the plan doc
# never wrote; corrected to the actual § C5 anchor.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core import coverage, session_attribution
from coordinator_core.ops import review_trail_write
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_PATH = Path(__file__).resolve().parent / "session_attribution_golden.json"


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


# ---------------------------------------------------------------------------
# A0 — golden fixture: P1 (`trailer_foreign_shas`) and the write guard's
# disposition (`_guard_foreign_session_range`), replayed against THIS repo's
# real history at real, pinned SHAs.
#
# docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md
# § Tasks, chunk A0 / AC5. The golden JSON
# (`coordinator_core/tests/session_attribution_golden.json`) was generated
# ONCE, from today's UNMODIFIED code, by
# `coordinator_core/tests/generate_session_attribution_golden.py` (committed
# alongside for auditable provenance) — this test reads the golden, it never
# regenerates it (fork-adjudication.md § 10.4: a test that regenerates its
# own oracle proves only self-consistency).
#
# MUST LAND FIRST relative to any P1/P2 production change (A1-A4): this is
# the oracle those chunks are reviewed against.
# ---------------------------------------------------------------------------


def _golden_git_run(args, cwd):
    """The same never-raises `GitRunner` contract the generator used."""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 2, "", str(exc)


def _classify_sha(sha: str, own_session_id: str) -> str:
    """Best-effort classification of WHY a golden sha differs, for a
    reviewer-facing diagnostic — never used to decide pass/fail, only to
    annotate a failure. Categories per this chunk's brief:
    `untrailered | merge | foreign-trailer | grep-only`.
    """
    rc, out, _err = _golden_git_run(
        ["git", "log", "-1", "--format=%P%x1f%(trailers:key=Session-Id,valueonly)", sha],
        str(_REPO_ROOT),
    )
    if rc != 0 or "\x1f" not in out:
        return "unknown"
    parents, trailer = out.strip("\n").split("\x1f", 1)
    if len(parents.split()) > 1:
        return "merge"
    trailer = trailer.strip()
    if not trailer:
        # No trailer ATOM — but a --grep over the message may still match
        # (the d21d8b0 shape: a Session-Id line present but not the last
        # message block, so git's own trailer parser ignores it).
        grep_rc, _grep_out, _grep_err = _golden_git_run(
            ["git", "log", "-1", f"--grep=^Session-Id: {own_session_id}$", sha],
            str(_REPO_ROOT),
        )
        return "grep-only" if grep_rc == 0 else "untrailered"
    return "foreign-trailer" if trailer != own_session_id else "own-trailer"


def _load_golden() -> dict:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def _golden_case_ids() -> list:
    return sorted(_load_golden()["cases"].keys())


@pytest.fixture(scope="module")
def golden() -> dict:
    return _load_golden()


def test_golden_pinned_head_is_reachable(golden):
    """The golden's basis commit must still be an ancestor of HEAD — history
    is append-only forward, so this only fails if history was rewritten."""
    rc, _out, _err = _golden_git_run(
        ["git", "merge-base", "--is-ancestor", golden["pinned_head_sha"], "HEAD"],
        str(_REPO_ROOT),
    )
    assert rc == 0, (
        f"golden's pinned_head_sha={golden['pinned_head_sha']!r} is no longer an "
        "ancestor of HEAD — history was rewritten; the golden's provenance no "
        "longer holds and must be regenerated (see generate_session_attribution_"
        "golden.py's module docstring)."
    )


@pytest.mark.parametrize("case_id", _golden_case_ids())
def test_trailer_foreign_shas_matches_golden(golden, case_id):
    case = golden["cases"][case_id]
    sha_range = case["sha_range"]
    own_session_id = case["own_session_id"]
    expected = case["trailer_foreign_shas"]

    if expected["ok"]:
        actual = session_attribution.trailer_foreign_shas(
            sha_range, own_session_id, str(_REPO_ROOT), {}, _golden_git_run,
        )
        actual_sorted = sorted(actual)
        expected_sorted = expected["foreign_shas"]
        if actual_sorted != expected_sorted:
            golden_set, actual_set = set(expected_sorted), set(actual_sorted)
            diff = golden_set ^ actual_set
            annotated = {
                sha: _classify_sha(sha, own_session_id) for sha in diff
            }
            pytest.fail(
                f"case={case_id!r} sha_range={sha_range!r} own_session_id="
                f"{own_session_id!r}: trailer_foreign_shas drifted from the golden.\n"
                f"golden={expected_sorted}\nactual={actual_sorted}\n"
                f"symmetric_difference (sha: classification)={annotated}"
            )
    else:
        with pytest.raises(session_attribution.GitLogFailed):
            session_attribution.trailer_foreign_shas(
                sha_range, own_session_id, str(_REPO_ROOT), {}, _golden_git_run,
            )


@pytest.mark.parametrize("case_id", _golden_case_ids())
def test_guard_disposition_matches_golden(golden, case_id):
    case = golden["cases"][case_id]
    sha_range = case["sha_range"]
    own_session_id = case["own_session_id"]
    expected = case["guard"]

    try:
        waived = review_trail_write._guard_foreign_session_range(
            sha_range, own_session_id, _REPO_ROOT,
        )
        actual = {"disposition": "proceed", "waived": sorted(waived)}
    except review_trail_write.ForeignSessionRangeRefused as exc:
        actual = {"disposition": "refused", "message": str(exc)}
    except ValueError as exc:
        actual = {"disposition": "error", "exception_type": "ValueError", "message": str(exc)}

    assert actual["disposition"] == expected["disposition"], (
        f"case={case_id!r} sha_range={sha_range!r} own_session_id={own_session_id!r}: "
        f"guard disposition drifted from the golden.\n"
        f"golden={expected}\nactual={actual}"
    )
    if expected["disposition"] == "proceed":
        assert actual["waived"] == expected["waived"], (
            f"case={case_id!r}: waived-sha set drifted from the golden.\n"
            f"golden={expected['waived']}\nactual={actual['waived']}"
        )
