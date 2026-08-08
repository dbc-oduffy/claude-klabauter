"""test_directives_commit_tail_peer_committed_paths — path-scoped regression
suite for `directives_commit_tail._peer_committed_paths`.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-
amplification-gate.md, `## Tasks` row `id: C18` ("`_peer_committed_paths`
migrates onto `bulk_trailer_session_map`"). No path-scoped test for this
function existed at HEAD; this file is that test, authored per the chunk's
own instruction to "verify, then write it".

**Finding this file backs (see C18's dispatch report, not restated here):**
the proposed migration onto `session_attribution.bulk_trailer_session_map`
does NOT hold as a byte-identical swap at this call site. Unlike C4's
`trailer_foreign_shas` -> `bulk_trailer_session_map` migration (an exact P1
equivalence — same `--no-merges` `git log`, same format string, same
`if sha and trailer` filter), `_peer_committed_paths` walks its window with
a bare `git log --since=<t> --format=%H` that includes MERGE commits, and
feeds each candidate sha through `archive_stamp._commit_session_id`'s
UUID-shape-validated single-commit trailer read before folding in that
commit's touched paths. `bulk_trailer_session_map` is `--no-merges` (§
Anti-scope 5/6 of the plan doc: "the looser semantics" — dropping it here
would convert a peer's merge-commit contribution into a silent miss, i.e.
UNDER-exclusion of a live peer's touched paths from `resolve_known_
concurrent_paths`'s exclusion set — the wrong direction for a function whose
own docstring states its correctness bar is "biased toward OVER-exclusion,
never under-exclusion"). `test_merge_commit_authored_by_peer_is_included`
below is the concrete pin for that divergence: it fails immediately if
`_peer_committed_paths` is ever rewritten to walk `--no-merges` history
(e.g. by delegating to `bulk_trailer_session_map`) without first solving the
merge-visibility gap that primitive does not have an answer for at this call
site.

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_peer_committed_paths.py -q
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.workstream_complete import directives_commit_tail


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _init_repo(path) -> str:
    """git-inits a fixture repo, returns the checked-out branch name (never
    assumed — `init.defaultBranch` varies across git installs/configs, and a
    hardcoded "main"/"master" here would make this fixture flaky exactly the
    way the plan's own § Anti-scope 19 warns against for unpinned state)."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=path).stdout.strip()
    return branch or "master"


def _commit(path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    _git("add", filename, cwd=path)
    _git("commit", "-qm", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    branch = _init_repo(root)
    sha = _commit(root, "seed.txt", "seed\n", "seed")
    if not sha:
        pytest.skip("git unavailable — cannot build a fixture repo with history")
    return root, branch


def _peer_committed_paths(repo_root, sid: str, monkeypatch, since_before) -> set:
    """Calls `_peer_committed_paths` with `resolve_session_start_time`
    monkeypatched to a fixed instant before every fixture commit, so the
    `--since=` window covers the whole fixture history regardless of wall-
    clock skew between fixture setup and the git-log call."""
    monkeypatch.setattr(
        directives_commit_tail._memo_lifecycle,
        "resolve_session_start_time",
        lambda _repo_root, _sid: since_before,
    )
    return directives_commit_tail._peer_committed_paths(repo_root, sid)


# ---------------------------------------------------------------------------
# Happy path — a plain (non-merge) commit trailered to the target sid
# ---------------------------------------------------------------------------


def test_trailered_commit_is_included(repo, monkeypatch):
    root, _branch = repo
    sid = "11111111-1111-1111-1111-111111111111"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "mine.txt", "mine\n", f"peer work\n\nSession-Id: {sid}\n")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "mine.txt" in result


# ---------------------------------------------------------------------------
# Untrailered commits are never attributed — the exclusion-based posture
# `trailer_foreign_shas`/`bulk_trailer_session_map` also document
# ---------------------------------------------------------------------------


def test_untrailered_commit_is_excluded(repo, monkeypatch):
    root, _branch = repo
    sid = "22222222-2222-2222-2222-222222222222"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "untrailered.txt", "x\n", "no trailer here")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "untrailered.txt" not in result


# ---------------------------------------------------------------------------
# A commit trailered to a DIFFERENT session is not attributed to sid
# ---------------------------------------------------------------------------


def test_other_session_commit_is_excluded(repo, monkeypatch):
    root, _branch = repo
    sid = "33333333-3333-3333-3333-333333333333"
    other_sid = "44444444-4444-4444-4444-444444444444"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "theirs.txt", "theirs\n", f"other peer work\n\nSession-Id: {other_sid}\n")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "theirs.txt" not in result


# ---------------------------------------------------------------------------
# UUID-shape validation (archive_stamp._commit_session_id) is load-bearing
# here even though the comparison itself is plain equality — a trailer that
# textually matches sid but is not UUID/hex-dash shaped must still be
# rejected. Distinguishes this call site's fail-closed reader from a naive
# equality-only comparison, per § Anti-scope 7's shape-validation warning
# for this primitive family.
# ---------------------------------------------------------------------------


def test_shape_invalid_trailer_is_excluded_despite_textual_match(repo, monkeypatch):
    root, _branch = repo
    # Underscore is not in `_SESSION_ID_UUID_RE`'s `[0-9a-fA-F-]` character
    # class, so this sid is intentionally shape-invalid.
    sid = "not_a_uuid_shape"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "shape-invalid.txt", "x\n", f"peer work\n\nSession-Id: {sid}\n")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "shape-invalid.txt" not in result


# ---------------------------------------------------------------------------
# THE key divergence test — a MERGE commit trailered to sid, with a real
# combined-diff-visible touched path (a resolved merge conflict), must be
# included under `_peer_committed_paths`'s current (`git log --since=...`,
# no `--no-merges`) behavior. `bulk_trailer_session_map` walks `--no-merges`
# and would silently drop this commit's contribution entirely — the finding
# this whole file backs. See the module docstring above.
# ---------------------------------------------------------------------------


def test_merge_commit_authored_by_peer_is_included(repo, monkeypatch):
    root, branch = repo
    sid = "55555555-5555-5555-5555-555555555555"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)

    _commit(root, "shared.txt", "A\n", "base shared")
    _git("checkout", "-qb", "feature", cwd=root)
    _commit(root, "shared.txt", "B\n", "feature edit")
    _git("checkout", "-q", branch, cwd=root)
    _commit(root, "shared.txt", "C\n", "base edit")

    merge_proc = _git("merge", "--no-ff", "-q", "feature", cwd=root)
    assert merge_proc.returncode != 0, "expected a merge conflict to set up the fixture"

    (root / "shared.txt").write_text("D\n", encoding="utf-8")
    _git("add", "shared.txt", cwd=root)
    commit_proc = _git(
        "commit", "-qm", f"resolve merge\n\nSession-Id: {sid}\n", cwd=root
    )
    assert commit_proc.returncode == 0, commit_proc.stderr

    merge_sha = _git("rev-parse", "HEAD", cwd=root).stdout.strip()
    parents = _git("log", "-1", "--format=%P", merge_sha, cwd=root).stdout.strip()
    assert len(parents.split()) > 1, "fixture HEAD must be a real 2-parent merge commit"

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "shared.txt" in result
