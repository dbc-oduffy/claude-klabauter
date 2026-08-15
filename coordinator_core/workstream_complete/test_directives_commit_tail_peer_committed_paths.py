"""test_directives_commit_tail_peer_committed_paths — path-scoped regression
suite for `directives_commit_tail._committed_paths_for_sids`, the sole
peer-attribution entry point production calls (via `resolve_known_
concurrent_paths`).

Spec backlink: docs/plans/2026-08-10-commit-event-5s-cap-and-the-silent-
tail.md, chunk C1. Originally authored against `docs/plans/2026-08-07-n-
plus-one-git-spawn-class-and-amplification-gate.md`'s C18 ("`_peer_committed_
paths` migrates onto `bulk_trailer_session_map`"), which was REVERTED —
`bulk_trailer_session_map` hardcoded `--no-merges`, silently dropping a
peer's merge-commit contribution (UNDER-exclusion; see
`test_merge_commit_authored_by_peer_is_included` below). C1 lands the fix
that C18 could not: `_committed_paths_for_sids` now calls
`bulk_trailer_session_map(..., include_merges=True)` — a parameterized
merge filter, default unchanged for every OTHER caller — so this file's
merge-inclusion pin still holds while the N+1 spawn shape it originally
also covered is now gone. `test_spawn_count_does_not_scale_with_commit_
count` is the new pin for that half.

Repointed off the former `_peer_committed_paths` single-sid convenience
wrapper (deleted — no production caller; see
state/debt-backlog/2026-08-11-peer-committed-paths-is-a-second-peer-at-
c596b80fa5db.yaml) onto `_committed_paths_for_sids` directly, so the tested
path and the shipped path (`resolve_known_concurrent_paths`'s own call) are
the same function. The single-sid convenience is reproduced in this file's
own `_peer_committed_paths` test helper below, which resolves `sid`'s start
time and unwraps the one-sid result — same call shape every test in this
file already used, no assertion intent changed.

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_peer_committed_paths.py -q
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.workstream_complete import directives_commit_tail

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


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
    """Calls `_committed_paths_for_sids` for a single sid — the shipped
    entry point `resolve_known_concurrent_paths` itself calls — with
    `resolve_session_start_time` monkeypatched to a fixed instant before
    every fixture commit, so the `--since=` window covers the whole fixture
    history regardless of wall-clock skew between fixture setup and the
    git-log call. Mirrors the former `_peer_committed_paths` single-sid
    convenience wrapper's own two steps (resolve start time, unwrap the
    one-sid result) so every existing assertion keeps its intent."""
    monkeypatch.setattr(
        directives_commit_tail._memo_lifecycle,
        "resolve_session_start_time",
        lambda _repo_root, _sid: since_before,
    )
    start = directives_commit_tail._memo_lifecycle.resolve_session_start_time(repo_root, sid)
    return directives_commit_tail._committed_paths_for_sids(repo_root, {sid: start}).get(sid, set())


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
# included under `_committed_paths_for_sids`'s current (`git log --since=...`,
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


# ---------------------------------------------------------------------------
# AC1 — the git-spawn count is bounded, independent of commit count. The
# pre-fix implementation spawned up to 2N processes (one `archive_stamp.
# _commit_session_id` per candidate sha, one `git show --name-only` per
# attributed sha) for a peer's own `--since=` window. This pins the fix:
# regardless of how many trailered commits sid authored, the number of real
# `subprocess.run` calls this module's own code makes must not grow with
# commit count.
# ---------------------------------------------------------------------------


def test_spawn_count_does_not_scale_with_commit_count(repo, monkeypatch):
    root, _branch = repo
    sid = "66666666-6666-6666-6666-666666666666"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(12):
        _commit(root, f"mine{i}.txt", f"mine{i}\n", f"peer work {i}\n\nSession-Id: {sid}\n")

    spawn_calls = []
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        spawn_calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert all(f"mine{i}.txt" in result for i in range(12))
    # Exactly two real `git` spawns for this call regardless of the 12
    # trailered commits above (one bulk trailer walk, one batched touched-
    # paths walk) — never one spawn per candidate/attributed sha.
    assert len(spawn_calls) == 2, [c[0] for c in spawn_calls]


# ---------------------------------------------------------------------------
# Overflow — a sha count ABOVE the chunking threshold still returns the
# COMPLETE union, split across multiple chunked spawns rather than one
# unchunked call or a per-sha spawn. Chunk size is monkeypatched down to a
# small value so the test does not need to build hundreds of real fixture
# commits to cross the threshold; the spawn-count assertion is what proves
# chunking actually happened (would fail if chunking were removed and the
# call collapsed back onto a single unchunked spawn).
# ---------------------------------------------------------------------------


def test_overflow_sha_count_returns_complete_union_via_chunked_spawns(repo, monkeypatch):
    root, _branch = repo
    sid = "77777777-7777-7777-7777-777777777777"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(directives_commit_tail, "_COMMITTED_PATHS_CHUNK", 2)
    commit_count = 5
    for i in range(commit_count):
        _commit(root, f"overflow{i}.txt", f"overflow{i}\n", f"peer work {i}\n\nSession-Id: {sid}\n")

    spawn_calls = []
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        spawn_calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert all(f"overflow{i}.txt" in result for i in range(commit_count))
    # 1 bulk trailer spawn + ceil(5 commits / chunk-of-2) == 3 chunked
    # touched-paths spawns == 4 total. A regression that removed chunking
    # (reverting to one unchunked spawn-2 call) would collapse this to 2.
    assert len(spawn_calls) == 4, [c[0] for c in spawn_calls]


# ---------------------------------------------------------------------------
# Fail-closed — a git failure on the batched touched-paths walk must NOT
# silently degrade to an empty peer-exclusion set (the pre-fix per-sha loop's
# fail-open posture). It must raise, surfacing the failure to the caller
# rather than reading as "confirmed no peer owns anything here".
# ---------------------------------------------------------------------------


def test_git_failure_on_touched_paths_walk_raises_not_empty(repo, monkeypatch):
    root, _branch = repo
    sid = "88888888-8888-8888-8888-888888888888"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "willfail.txt", "x\n", f"peer work\n\nSession-Id: {sid}\n")

    # `_chunked_committed_paths` now calls `_run_git_ok_retrying`, not
    # `_run_git_ok` directly (bounded retry wrapper added for routine lock
    # contention — see that function's own docstring). Patched at that
    # layer so this fail-closed pin exercises the actual call site.
    monkeypatch.setattr(directives_commit_tail, "_run_git_ok_retrying", lambda *_a, **_k: None)

    with pytest.raises(directives_commit_tail.PeerAttributionUnavailable):
        _peer_committed_paths(root, sid, monkeypatch, since_before)
